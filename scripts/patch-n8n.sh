#!/usr/bin/env bash
# Патчи для n8n 2.8.4:
#   1. Разблокировка Enterprise-фич (license.js, license-state.js)
#   2. Фикс двойного /n8n/ префикса в SPA-навигации (WorkflowsView)
#
# Идемпотентный: повторный запуск не ломает уже пропатченные файлы.
set -e

N8N_DIR="$(npm root -g)/n8n"

if [ ! -d "$N8N_DIR" ]; then
  echo "ERROR: n8n not found at $N8N_DIR"
  exit 1
fi

echo "==> n8n dir: $N8N_DIR"

# ---------------------------------------------------------------------------
# 1. license.js — isLicensed, isAPIDisabled, getAiCredits, getTeamProjectLimit, getPlanName
# ---------------------------------------------------------------------------
LICENSE_JS="$N8N_DIR/dist/license.js"

python3 - "$LICENSE_JS" << 'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
src = path.read_text()

patches = [
    (
        'isLicensed(feature) { return this.manager?.hasFeatureEnabled(feature) ?? false; }',
        'isLicensed(feature) { return true; }'
    ),
    (
        'isAPIDisabled() { return this.isLicensed(constants_1.LICENSE_FEATURES.API_DISABLED); }',
        'isAPIDisabled() { return false; }'
    ),
    (
        'getAiCredits() { return this.getValue(constants_1.LICENSE_QUOTAS.AI_CREDITS) ?? 0; }',
        'getAiCredits() { return constants_1.UNLIMITED_LICENSE_QUOTA; }'
    ),
    (
        'getTeamProjectLimit() { return this.getValue(constants_1.LICENSE_QUOTAS.TEAM_PROJECT_LIMIT) ?? 0; }',
        'getTeamProjectLimit() { return constants_1.UNLIMITED_LICENSE_QUOTA; }'
    ),
    (
        "getPlanName() { return this.getValue('planName') ?? 'Community'; }",
        "getPlanName() { return 'Enterprise'; }"
    ),
]

changed = False
for old, new in patches:
    if old in src:
        src = src.replace(old, new)
        changed = True
        print(f'  patched: {old[:60]}...')
    elif new in src:
        print(f'  already patched: {new[:60]}...')
    else:
        print(f'  WARNING: pattern not found: {old[:60]}...')

if changed:
    path.with_suffix('.js.bak').write_text(path.read_bytes().decode('utf-8', errors='replace'))
    path.write_text(src)
    print(f'  saved: {path}')
PYEOF

# ---------------------------------------------------------------------------
# 2. license-state.js — LicenseState quotas
# ---------------------------------------------------------------------------
LICENSE_STATE_JS="$N8N_DIR/node_modules/@n8n/backend-common/dist/license-state.js"

python3 - "$LICENSE_STATE_JS" << 'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
src = path.read_text()

patches = [
    (
        "isAPIDisabled() { return this.isLicensed('feat:apiDisabled'); }",
        "isAPIDisabled() { return false; }"
    ),
    (
        "getMaxTeamProjects() { return this.getValue('quota:maxTeamProjects') ?? 0; }",
        "getMaxTeamProjects() { return constants_1.UNLIMITED_LICENSE_QUOTA; }"
    ),
    (
        "getMaxAiCredits() { return this.getValue('quota:aiCredits') ?? 0; }",
        "getMaxAiCredits() { return constants_1.UNLIMITED_LICENSE_QUOTA; }"
    ),
    (
        "getMaxWorkflowsWithEvaluations() { return this.getValue('quota:evaluations:maxWorkflows') ?? 0; }",
        "getMaxWorkflowsWithEvaluations() { return constants_1.UNLIMITED_LICENSE_QUOTA; }"
    ),
    (
        "getInsightsMaxHistory() { return this.getValue('quota:insights:maxHistoryDays') ?? 7; }",
        "getInsightsMaxHistory() { return 365; }"
    ),
]

changed = False
for old, new in patches:
    if old in src:
        src = src.replace(old, new)
        changed = True
        print(f'  patched: {old[:60]}...')
    elif new in src:
        print(f'  already patched: {new[:60]}...')
    else:
        print(f'  WARNING: pattern not found: {old[:60]}...')

if changed:
    path.with_suffix('.js.bak').write_text(path.read_bytes().decode('utf-8', errors='replace'))
    path.write_text(src)
    print(f'  saved: {path}')
PYEOF

# ---------------------------------------------------------------------------
# 3. WorkflowsView — router.resolve().href → .fullPath (фикс двойного /n8n/)
# ---------------------------------------------------------------------------
WORKFLOWS_VIEW=$(find "$N8N_DIR/node_modules/n8n-editor-ui/dist/assets" \
  -name 'WorkflowsView-*.js' ! -name '*.bak' 2>/dev/null | head -1)

if [ -z "$WORKFLOWS_VIEW" ]; then
  echo "  WARNING: WorkflowsView-*.js not found, skipping"
else
  python3 - "$WORKFLOWS_VIEW" << 'PYEOF'
import sys, pathlib, re

path = pathlib.Path(sys.argv[1])
src = path.read_text()

# getFolderUrl возвращал .href (включает base /n8n), что дублировало префикс
# при router.push() и RouterLink to=...
# Заменяем .href → .fullPath для всех router.resolve() в этом файле,
# кроме window.open (там нужен полный путь).

count_semi  = src.count('}).href;')   # присвоения переменным
count_nl    = src.count('}).href\n')  # свойства объектов (breadcrumbs)

if count_semi == 0 and count_nl == 0:
    # Проверяем, не пропатчен ли уже
    if '}).fullPath;' in src or '}).fullPath\n' in src:
        print(f'  already patched: {path.name}')
    else:
        print(f'  WARNING: patterns not found in {path.name}')
    sys.exit(0)

new_src = src.replace('}).href;', '}).fullPath;').replace('}).href\n', '}).fullPath\n')
path.with_suffix('.js.bak').write_text(src)
path.write_text(new_src)
print(f'  patched {count_semi} variable assignments + {count_nl} object properties in {path.name}')
PYEOF
fi

echo "==> patch-n8n.sh done"
