#!/usr/bin/env python3
"""
Bootstrap Railway via API GraphQL.

Exécuté par .github/workflows/bootstrap-railway.yml.
"""

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

# Railway a deux domaines historiques : railway.com et railway.app
# Le backboard GraphQL est actuellement sur backboard.railway.com
API_CANDIDATES = [
    'https://backboard.railway.com/graphql/v2',
    'https://backboard.railway.app/graphql/v2',
]

API = None  # determine au runtime

RAILWAY_TOKEN = os.environ['RAILWAY_TOKEN']
GH_REPO = os.environ['GH_REPO']
GH_REPO_OWNER = os.environ.get('GH_REPO_OWNER', GH_REPO.split('/')[0])
PROJECT_NAME = os.environ.get('PROJECT_NAME', 'my-new-project')

USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 KlideBootstrap/1.0'
)


def log(msg: str) -> None:
    print(f'[bootstrap] {msg}', flush=True)


def step(n: int, label: str) -> None:
    print(f'\n=== STEP {n}: {label} ===', flush=True)


def gen_secret(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def gql_raw(api_url: str, query: str, variables: dict | None = None) -> tuple[int, str]:
    """Send a GraphQL request and return (status_code, body_text). Retries on 5xx and timeouts."""
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    delays = [0, 8, 20, 45, 90]  # backoff exponentiel
    last_error = None
    for attempt, delay in enumerate(delays):
        if delay > 0:
            log(f'  Retry attempt {attempt + 1}/{len(delays)} apres {delay}s...')
            time.sleep(delay)
        req = urllib.request.Request(api_url, data=body, method='POST')
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
                last_error = f'HTTP {e.code}'
                continue
            return e.code, body_text
        except (TimeoutError, OSError) as e:
            last_error = str(e)
            if attempt < len(delays) - 1:
                continue
            return 599, f'Network error after retries: {last_error}'
    return 599, f'All retries exhausted: {last_error}'


def discover_api() -> str:
    """Find which Railway API endpoint responds correctly to a simple query."""
    test_query = 'query { __typename }'
    for url in API_CANDIDATES:
        log(f'Probe {url}')
        try:
            status, body = gql_raw(url, test_query)
            log(f'  -> HTTP {status}, body (first 200 chars): {body[:200]}')
            if status == 200 and '"data"' in body:
                log(f'  OK : {url} repond, je l utilise.')
                return url
        except Exception as e:
            log(f'  Exception: {e}')
    raise SystemExit('Aucun endpoint Railway GraphQL ne repond')


def gql(query: str, variables: dict | None = None) -> dict:
    """Send a GraphQL request to the chosen API. Raises on errors."""
    assert API is not None, 'API non determine'
    status, body = gql_raw(API, query, variables)
    if status != 200:
        raise SystemExit(f'HTTP {status} from Railway: {body[:1000]}')
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(f'Reponse non-JSON de Railway:\n{body[:500]}')
    if 'errors' in payload and payload['errors']:
        raise SystemExit(f'GraphQL errors:\n{json.dumps(payload["errors"], indent=2)}')
    return payload.get('data', {})


def find_project(name: str) -> dict | None:
    """Look for an existing project by name. Tries multiple query paths."""
    # Path 1 : me.projects (peut ne pas retourner les projets workspace personnel)
    try:
        data = gql('''
            query Me {
                me {
                    projects {
                        edges {
                            node {
                                id
                                name
                                environments { edges { node { id name } } }
                                services { edges { node { id name } } }
                            }
                        }
                    }
                }
            }
        ''')
        for edge in data.get('me', {}).get('projects', {}).get('edges', []):
            proj = edge['node']
            if proj['name'] == name:
                return proj
    except SystemExit:
        pass

    # Path 2 : iterate via workspaces
    try:
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
                                        environments { edges { node { id name } } }
                                        services { edges { node { id name } } }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        ''')
        for ws in data.get('me', {}).get('workspaces', []):
            team = ws.get('team') or {}
            for edge in (team.get('projects') or {}).get('edges', []):
                proj = edge['node']
                if proj['name'] == name:
                    return proj
    except SystemExit:
        pass

    # Path 3 : query projects top-level
    try:
        data = gql('''
            query Projects {
                projects {
                    edges {
                        node {
                            id
                            name
                            environments { edges { node { id name } } }
                            services { edges { node { id name } } }
                        }
                    }
                }
            }
        ''')
        for edge in data.get('projects', {}).get('edges', []):
            proj = edge['node']
            if proj['name'] == name:
                return proj
    except SystemExit:
        pass

    return None


def get_workspace_id() -> str:
    """Find the user's workspace id. Railway 'workspaces' is a direct list, not paginated."""
    # Tentative 1 : me { workspaces { id name } } (array direct)
    try:
        data = gql('query { me { workspaces { id name } } }')
        workspaces = data.get('me', {}).get('workspaces', [])
        if workspaces:
            ws = workspaces[0]
            log(f'Workspace : {ws["name"]} ({ws["id"]})')
            return ws['id']
    except SystemExit as e:
        log(f'Forme array a echoue : {str(e)[:200]}')

    # Tentative 2 : me { workspace { id name } } (singulier)
    try:
        data = gql('query { me { workspace { id name } } }')
        ws = data.get('me', {}).get('workspace')
        if ws:
            log(f'Workspace singulier : {ws["name"]} ({ws["id"]})')
            return ws['id']
    except SystemExit as e:
        log(f'Forme singulier a echoue : {str(e)[:200]}')

    raise SystemExit('Aucun workspace Railway trouve pour ce token')


def create_project(name: str, workspace_id: str) -> dict:
    data = gql('''
        mutation ProjectCreate($input: ProjectCreateInput!) {
            projectCreate(input: $input) {
                id
                name
                environments { edges { node { id name } } }
            }
        }
    ''', {
        'input': {
            'name': name,
            'description': 'Klide.ai - GEO v2 (auto-provisioned)',
            'workspaceId': workspace_id,
        },
    })
    return data['projectCreate']


def get_environment_id(project: dict) -> str:
    envs = project.get('environments', {}).get('edges', [])
    for edge in envs:
        if edge['node']['name'] == 'production':
            return edge['node']['id']
    if envs:
        return envs[0]['node']['id']
    raise SystemExit('Aucun environnement trouve sur le projet')


def list_services(project_id: str) -> list[dict]:
    data = gql('''
        query Project($id: String!) {
            project(id: $id) {
                services { edges { node { id name } } }
            }
        }
    ''', {'id': project_id})
    return [e['node'] for e in data['project']['services']['edges']]


def create_mongodb_service(project_id: str) -> dict:
    data = gql('''
        mutation ServiceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) { id name }
        }
    ''', {
        'input': {
            'projectId': project_id,
            'name': 'MongoDB',
            'source': {'image': 'mongo:7'},
        }
    })
    s = data['serviceCreate']
    log(f'MongoDB cree : {s["id"]}')
    return s


def create_web_service(project_id: str, repo: str) -> dict:
    data = gql('''
        mutation ServiceCreate($input: ServiceCreateInput!) {
            serviceCreate(input: $input) { id name }
        }
    ''', {
        'input': {
            'projectId': project_id,
            'name': 'web',
            'source': {'repo': repo},
            'branch': 'main',
        }
    })
    s = data['serviceCreate']
    log(f'Web cree : {s["id"]}')
    return s


def set_variable(project_id: str, environment_id: str, service_id: str, name: str, value: str) -> None:
    gql('''
        mutation VariableUpsert($input: VariableUpsertInput!) {
            variableUpsert(input: $input)
        }
    ''', {
        'input': {
            'projectId': project_id,
            'environmentId': environment_id,
            'serviceId': service_id,
            'name': name,
            'value': value,
        }
    })


def trigger_deploy(service_id: str, environment_id: str) -> str:
    data = gql('''
        mutation Deploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
        }
    ''', {'serviceId': service_id, 'environmentId': environment_id})
    return str(data.get('serviceInstanceDeployV2', ''))


def generate_domain(service_id: str, environment_id: str) -> str:
    data = gql('''
        mutation DomainCreate($input: ServiceDomainCreateInput!) {
            serviceDomainCreate(input: $input) { id domain }
        }
    ''', {
        'input': {
            'serviceId': service_id,
            'environmentId': environment_id,
            'targetPort': 3000,
        }
    })
    return data['serviceDomainCreate']['domain']


def get_existing_domain(service_id: str, environment_id: str) -> str | None:
    try:
        data = gql('''
            query Domains($serviceId: String!, $environmentId: String!) {
                serviceDomains(serviceId: $serviceId, environmentId: $environmentId) {
                    edges { node { domain } }
                }
            }
        ''', {'serviceId': service_id, 'environmentId': environment_id})
        edges = data.get('serviceDomains', {}).get('edges', [])
        if edges:
            return edges[0]['node']['domain']
    except SystemExit:
        pass  # query peut ne pas exister, on s'en fout
    return None


def main() -> int:
    global API

    step(0, 'Discover Railway API endpoint')
    API = discover_api()

    step(1, 'Verifier l auth Railway')
    me_data = gql('query { me { id email name } }')
    me = me_data.get('me', {})
    log(f'Authentifie en tant que : email={me.get("email")} name={me.get("name")} id={me.get("id")}')

    step(2, f'Trouver ou creer le projet "{PROJECT_NAME}"')
    project = find_project(PROJECT_NAME)
    if project:
        log(f'Projet existant : {project["id"]}')
    else:
        log('Aucun projet existant.')
        workspace_id = get_workspace_id()
        log(f'Creation du projet dans workspace {workspace_id}...')
        project = create_project(PROJECT_NAME, workspace_id)
        log(f'Projet cree : {project["id"]}')
        # On garde l'objet retourne par la mutation (avec ses environments) sans re-fetch
        # car find_project peut ne pas voir le projet immediatement.

    project_id = project['id']
    environment_id = get_environment_id(project)
    log(f'environment_id (production) : {environment_id}')

    step(3, 'Services existants')
    services = list_services(project_id)
    log(f'Services : {[s["name"] for s in services]}')
    mongo_service = next((s for s in services if s['name'].lower() in ('mongodb', 'mongo')), None)
    web_service = next((s for s in services if s['name'].lower() == 'web'), None)

    step(4, 'MongoDB')
    if mongo_service:
        log(f'Existant : {mongo_service["id"]}')
    else:
        mongo_service = create_mongodb_service(project_id)

    step(5, 'Service web (GitHub)')
    if web_service:
        log(f'Existant : {web_service["id"]}')
    else:
        web_service = create_web_service(project_id, GH_REPO)

    step(6, 'Variables d environnement')
    mongo_var_name = mongo_service['name'].upper().replace('-', '_')
    mongo_url_ref = f'${{{{{mongo_var_name}.MONGO_URL}}}}'
    nextauth_secret = gen_secret(32)
    variables = {
        'MONGODB_URI': mongo_url_ref,
        'NEXTAUTH_SECRET': nextauth_secret,
        'KLIDE_FAKE_LLMS': 'true',
        'NODE_ENV': 'production',
        'PORT': '3000',
    }
    for name, value in variables.items():
        log(f'  set {name}')
        set_variable(project_id, environment_id, web_service['id'], name, value)

    step(7, 'Domaine public')
    domain = get_existing_domain(web_service['id'], environment_id)
    if domain:
        log(f'Existant : {domain}')
    else:
        domain = generate_domain(web_service['id'], environment_id)
        log(f'Cree : {domain}')
    url = f'https://{domain}'
    log(f'  set NEXTAUTH_URL = {url}')
    set_variable(project_id, environment_id, web_service['id'], 'NEXTAUTH_URL', url)

    step(8, 'Trigger deploy')
    try:
        deploy_id = trigger_deploy(web_service['id'], environment_id)
        log(f'Deploy : {deploy_id}')
    except SystemExit as e:
        log(f'Deploy trigger non-bloquant : {e}')

    step(9, 'URL finale')
    log(f'URL = {url}')
    with open('/tmp/klide_url.txt', 'w') as f:
        f.write(url)
    print(f'\n\n   DEPLOYED URL = {url}\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise SystemExit(f'Exception non geree : {e}')
