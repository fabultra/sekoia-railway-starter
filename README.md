# Sekoia Railway Starter

Template repo pour bootstrap un nouveau projet sur Railway en quelques minutes, sans aucune intervention manuelle apres le clone.

## Usage rapide

### Option A : Via Claude Desktop (recommandee pour Fabien)

Dis simplement a Claude :

> Deploie un nouveau projet `mon-projet` sur Railway depuis le template sekoia-railway-starter.

Claude :
1. Clone ce template via API GitHub (genere un nouveau repo)
2. Stocke `RAILWAY_TOKEN` (lu depuis `~/Documents/sekoia-secrets/tokens.env`) comme secret du nouveau repo
3. Trigger le workflow `bootstrap-railway` avec `project_name=mon-projet`
4. Poll jusqu a completion (5-10 min)
5. Retourne l URL de l app deployee

### Option B : Manuelle via GitHub UI

1. Clique **Use this template** -> Create a new repository
2. Va dans le nouveau repo -> Settings -> Secrets and variables -> Actions -> New repository secret
   - Name : `RAILWAY_TOKEN`
   - Value : ton token Railway personnel (`https://railway.com/account/tokens`)
3. Actions -> Bootstrap Railway -> Run workflow -> indique `project_name`
4. Attends 5-10 min, l URL apparait dans le run summary

## Ce que ce template provisionne

- 1 projet Railway dans ton workspace par defaut
- 1 service MongoDB (image `mongo:7`)
- 1 service web lie au repo GitHub courant (deploy auto sur push main)
- Variables d env : `MONGODB_URI` (ref au service MongoDB), `NEXTAUTH_SECRET` (genere), `NEXTAUTH_URL` (avec domain Railway), `NODE_ENV=production`, `PORT=3000`
- 1 domaine public Railway genere automatiquement
- Healthcheck sur `/api/health`

## Stack par defaut

Le Dockerfile suppose une app Next.js avec `output: standalone`. Adapte le Dockerfile pour ta stack si besoin (Vite, Astro, Bun, etc.).

## Workflows inclus

- `bootstrap-railway.yml` : provisionne le projet Railway initial (a executer une fois)
- `railway-ops.yml` : operations courantes (list-projects, delete-project, deploy-logs, redeploy, add-service, full-diagnostic)

## Operations courantes apres le bootstrap

```
# Voir tous tes projets Railway
trigger railway-ops avec action=list-projects

# Lire les logs du dernier deploy
trigger railway-ops avec action=deploy-logs et SERVICE_ID + ENVIRONMENT_ID

# Ajouter Redis a un projet existant
trigger railway-ops avec action=add-service, PROJECT_ID, SERVICE_NAME=Redis, SERVICE_IMAGE=redis:7-alpine

# Trigger un redeploy
trigger railway-ops avec action=redeploy et SERVICE_ID + ENVIRONMENT_ID

# Supprimer un projet
trigger railway-ops avec action=delete-project et PROJECT_ID
```

## Personnalisation

### Changer le projet par defaut
Edite `.github/scripts/railway_bootstrap.py` ligne `PROJECT_NAME = os.environ.get(...)` (deja parametre via env var).

### Ajouter d autres variables d env
Edite la fonction `main()` dans `.github/scripts/railway_bootstrap.py`, section "Variables d environnement".

### Changer la stack DB
Modifie l image dans `create_mongodb_service()` ou ajoute une fonction `create_postgres_service()` similaire.

### Customer port
Modifie `.env.example` et la variable `PORT` dans le script.

## Erreurs courantes resolues automatiquement

Le script gere deja :
- Cloudflare 1010 (bot detection) : User-Agent Mozilla
- Cloudflare 504 (origin timeout) : retry+backoff `[0, 8, 20, 45, 90]`s
- `workspaceId` requis : auto-discovery via `me.workspaces`
- Schema GraphQL variant : tentatives multiples (workspaces array vs paginated)
- Logs GitHub Actions inaccessibles : workflow commit ses propres logs dans `.ci-logs/`

## Liens utiles

- Skill complet : `~/.claude/skills/railway-automation/SKILL.md`
- Tokens : `~/Documents/sekoia-secrets/tokens.env`
- Doc PATs : `~/.claude/skills/railway-automation/checklists/github-pat-scopes.md`
- API Railway : `https://docs.railway.com/reference/public-api`

## Licence

MIT (template open-source pour Sekoia et toute personne qui le clone).
