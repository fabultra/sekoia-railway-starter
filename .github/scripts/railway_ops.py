#!/usr/bin/env python3
"""
Operations Railway via API GraphQL : list / delete / logs / redeploy.

Pilote par variable d'env ACTION :
- ACTION=list-projects      : liste tous les projets accessibles
- ACTION=delete-project     : supprime un projet (PROJECT_ID requis)
- ACTION=list-services      : liste les services d un projet (PROJECT_ID requis)
- ACTION=delete-service     : supprime un service (SERVICE_ID requis)
- ACTION=deploy-logs        : recupere les logs du dernier deploy (SERVICE_ID, ENVIRONMENT_ID requis)
- ACTION=redeploy           : trigger un nouveau deploy (SERVICE_ID, ENVIRONMENT_ID requis)
- ACTION=full-diagnostic    : tout faire en un coup (list projets + leur services + deploy logs)

Variables d'env requises :
- RAILWAY_TOKEN
- ACTION
- PROJECT_ID, SERVICE_ID, ENVIRONMENT_ID selon action

Output : ecrit dans /tmp/railway-ops-output.txt (commit dans .ci-logs/last-ops.log).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = 'https://backboard.railway.com/graphql/v2'
RAILWAY_TOKEN = os.environ['RAILWAY_TOKEN']
ACTION = os.environ.get('ACTION', 'list-projects')

USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 SekoiaOps/1.0'
)


def log(msg: str) -> None:
    print(msg, flush=True)


def gql_raw(query: str, variables: dict | None = None) -> tuple[int, str]:
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    delays = [0, 5, 15, 45, 90]
    last = None
    for attempt, d in enumerate(delays):
        if d:
            log(f'  Retry {attempt + 1}/{len(delays)} dans {d}s...')
            time.sleep(d)
        req = urllib.request.Request(API, data=body, method='POST')
        req.add_header('Authorization', f'Bearer {RAILWAY_TOKEN}')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            if e.code in (502, 503, 504) and attempt < len(delays) - 1:
                last = f'HTTP {e.code}'
                continue
            return e.code, body_text
        except (TimeoutError, OSError) as e:
            last = str(e)
            if attempt < len(delays) - 1:
                continue
            return 599, str(e)
    return 599, last or 'unknown'


def gql(query: str, variables: dict | None = None) -> dict:
    status, body = gql_raw(query, variables)
    if status != 200:
        raise SystemExit(f'HTTP {status}: {body[:500]}')
    payload = json.loads(body)
    if 'errors' in payload and payload['errors']:
        raise SystemExit(f'GraphQL errors:\n{json.dumps(payload["errors"], indent=2)}')
    return payload.get('data', {})


def list_all_projects() -> list[dict]:
    """Liste TOUS les projets accessibles via le workspace de l'utilisateur."""
    data = gql('''
        query Me {
            me {
                workspaces {
                    id
                    name
                    team {
                        projects {
                            edges {
                                node {
                                    id
                                    name
                                    description
                                    createdAt
                                    services { edges { node { id name } } }
                                    environments { edges { node { id name } } }
                                }
                            }
                        }
                    }
                }
            }
        }
    ''')
    projects = []
    for ws in data.get('me', {}).get('workspaces', []):
        team = ws.get('team') or {}
        for edge in (team.get('projects') or {}).get('edges', []):
            p = edge['node']
            p['_workspace'] = ws['name']
            projects.append(p)
    return projects


def delete_project(project_id: str) -> bool:
    log(f'  Suppression projet {project_id}...')
    data = gql('mutation Del($id: String!) { projectDelete(id: $id) }', {'id': project_id})
    return bool(data.get('projectDelete'))


def list_services(project_id: str) -> list[dict]:
    data = gql('''
        query Project($id: String!) {
            project(id: $id) {
                id
                name
                services { edges { node { id name updatedAt } } }
                environments { edges { node { id name } } }
            }
        }
    ''', {'id': project_id})
    return data.get('project', {})


def get_latest_deploy_logs(service_id: str, environment_id: str) -> str:
    """Recupere les logs (build + deploy) du dernier deploiement d un service."""
    # Method 1 : query deployments via service
    try:
        data = gql('''
            query ServiceDeployments($id: String!) {
                service(id: $id) {
                    deployments(first: 3) {
                        edges {
                            node {
                                id
                                status
                                createdAt
                                environmentId
                            }
                        }
                    }
                }
            }
        ''', {'id': service_id})
        deployments = (data.get('service') or {}).get('deployments', {}).get('edges', [])
    except SystemExit:
        deployments = []

    if not deployments:
        # Method 2 : query deployments via input list
        try:
            data = gql('''
                query Deployments($input: DeploymentListInput!) {
                    deployments(input: $input, first: 5) {
                        edges {
                            node {
                                id
                                status
                                createdAt
                            }
                        }
                    }
                }
            ''', {'input': {'environmentId': environment_id, 'serviceId': service_id}})
            deployments = data.get('deployments', {}).get('edges', [])
        except SystemExit:
            deployments = []

    if not deployments:
        return '(aucun deploiement trouve via methodes connues)'

    # Filtrer par environment si specifie
    matched = [d for d in deployments if d['node'].get('environmentId') in (environment_id, None)]
    target = matched[0] if matched else deployments[0]
    deployment_id = target['node']['id']
    deployment_status = target['node']['status']
    log(f'    Deployment cible : {deployment_id} status={deployment_status}')

    # Build logs
    try:
        data = gql('''
            query BuildLogs($id: String!) {
                buildLogs(deploymentId: $id, limit: 500) {
                    timestamp
                    message
                    severity
                }
            }
        ''', {'id': deployment_id})
        bl = data.get('buildLogs') or []
        build_lines = [f"[{e.get('severity', 'INFO')}] {e.get('message', '')}" for e in bl]
        build_log = '\n'.join(build_lines) if build_lines else '(pas de build log)'
    except SystemExit as e:
        build_log = f'(build log query error: {str(e)[:200]})'

    # Deploy/runtime logs
    try:
        data = gql('''
            query DeployLogs($id: String!) {
                deploymentLogs(deploymentId: $id, limit: 200) {
                    timestamp
                    message
                    severity
                }
            }
        ''', {'id': deployment_id})
        dl = data.get('deploymentLogs') or []
        deploy_lines = [f"[{e.get('severity', 'INFO')}] {e.get('message', '')}" for e in dl]
        deploy_log = '\n'.join(deploy_lines) if deploy_lines else '(pas de deploy log)'
    except SystemExit as e:
        deploy_log = f'(deploy log query error: {str(e)[:200]})'

    return f'=== DEPLOY {deployment_id} - STATUS: {deployment_status} ===\n\n=== BUILD LOG ===\n{build_log[-8000:]}\n\n=== RUNTIME LOG ===\n{deploy_log[-3000:]}'


def redeploy(service_id: str, environment_id: str) -> str:
    data = gql('''
        mutation Deploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
        }
    ''', {'serviceId': service_id, 'environmentId': environment_id})
    return str(data.get('serviceInstanceDeployV2', ''))


def main() -> int:
    out_lines = [f'=== ACTION: {ACTION} ===', f'=== {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())} ===', '']

    if ACTION == 'list-projects':
        projects = list_all_projects()
        out_lines.append(f'Trouve {len(projects)} projet(s) :\n')
        for p in projects:
            services = [s['node']['name'] for s in (p.get('services') or {}).get('edges', [])]
            envs = [e['node']['name'] for e in (p.get('environments') or {}).get('edges', [])]
            out_lines.append(f'- "{p["name"]}" (id: {p["id"]})')
            out_lines.append(f'   workspace: {p["_workspace"]}')
            out_lines.append(f'   created:   {p.get("createdAt")}')
            out_lines.append(f'   services:  {services}')
            out_lines.append(f'   envs:      {envs}')
            out_lines.append('')

    elif ACTION == 'delete-project':
        pid = os.environ['PROJECT_ID']
        ok = delete_project(pid)
        out_lines.append(f'Delete {pid}: {"OK" if ok else "FAIL"}')

    elif ACTION == 'list-services':
        pid = os.environ['PROJECT_ID']
        project = list_services(pid)
        out_lines.append(json.dumps(project, indent=2))

    elif ACTION == 'deploy-logs':
        sid = os.environ['SERVICE_ID']
        eid = os.environ['ENVIRONMENT_ID']
        out_lines.append(get_latest_deploy_logs(sid, eid))

    elif ACTION == 'redeploy':
        sid = os.environ['SERVICE_ID']
        eid = os.environ['ENVIRONMENT_ID']
        deploy_id = redeploy(sid, eid)
        out_lines.append(f'Redeploy triggered: {deploy_id}')

    elif ACTION == 'add-service':
        pid = os.environ['PROJECT_ID']
        sname = os.environ['SERVICE_NAME']
        simage = os.environ.get('SERVICE_IMAGE', 'redis:7-alpine')
        data = gql('''
            mutation ServiceCreate($input: ServiceCreateInput!) {
                serviceCreate(input: $input) { id name }
            }
        ''', {'input': {'projectId': pid, 'name': sname, 'source': {'image': simage}}})
        s = data['serviceCreate']
        out_lines.append(f'Service "{sname}" ({simage}) cree : {s["id"]}')

    elif ACTION == 'full-diagnostic':
        projects = list_all_projects()
        klide_projects = [p for p in projects if 'klide' in p['name'].lower()]
        out_lines.append(f'Projets "klide*" trouves : {len(klide_projects)}\n')
        for p in klide_projects:
            services = [(s['node']['id'], s['node']['name']) for s in (p.get('services') or {}).get('edges', [])]
            envs = {e['node']['name']: e['node']['id'] for e in (p.get('environments') or {}).get('edges', [])}
            prod_env = envs.get('production') or list(envs.values())[0] if envs else None
            out_lines.append(f'== Projet "{p["name"]}" ({p["id"]})')
            out_lines.append(f'   created: {p.get("createdAt")}')
            out_lines.append(f'   services: {[s[1] for s in services]}')
            for sid, sname in services:
                if sname.lower() == 'web' and prod_env:
                    out_lines.append(f'   --- Logs web service ---')
                    try:
                        logs = get_latest_deploy_logs(sid, prod_env)
                        out_lines.append(logs)
                    except SystemExit as e:
                        out_lines.append(f'   (erreur logs: {e})')
            out_lines.append('')

    else:
        out_lines.append(f'Action inconnue : {ACTION}')

    output = '\n'.join(out_lines)
    print(output)
    with open('/tmp/railway-ops-output.txt', 'w') as f:
        f.write(output)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise SystemExit(f'Exception: {e}')
