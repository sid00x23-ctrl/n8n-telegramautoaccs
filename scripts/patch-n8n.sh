#!/usr/bin/env bash
# Патчи для n8n 2.32.x:
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
echo "==> n8n version: $(node -e "console.log(require('$N8N_DIR/package.json').version)" 2>/dev/null || echo 'unknown')"

# ---------------------------------------------------------------------------
# 1. license.js — isLicensed, isAPIDisabled, getAiCredits, getTeamProjectLimit, getPlanName
#
# В 2.32.x код многострочный, поэтому патчим отдельные return-строки внутри функций.
# Каждая строка уникальна в файле — замена безопасна.
# ---------------------------------------------------------------------------
LICENSE_JS="$N8N_DIR/dist/license.js"

python3 - "$LICENSE_JS" << 'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
src = path.read_text()

# Каждый патч: (старая строка, новая строка)
# Строки уникальны в файле — простая замена без regex.
patches = [
    (
        "return this.manager?.hasFeatureEnabled(feature) ?? false;",
        "return true;"
    ),
    (
        "return this.isLicensed(constants_1.LICENSE_FEATURES.API_DISABLED);",
        "return false;"
    ),
    (
        "return this.getValue(constants_1.LICENSE_QUOTAS.AI_CREDITS) ?? 0;",
        "return constants_1.UNLIMITED_LICENSE_QUOTA;"
    ),
    (
        "return this.getValue(constants_1.LICENSE_QUOTAS.TEAM_PROJECT_LIMIT) ?? 0;",
        "return constants_1.UNLIMITED_LICENSE_QUOTA;"
    ),
    (
        "return this.getValue('planName') ?? 'Community';",
        "return 'Enterprise';"
    ),
]

changed = False
for old, new in patches:
    if old in src:
        # Сохраняем отступ оригинальной строки
        lines = src.split('\n')
        for i, line in enumerate(lines):
            if old in line:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = indent + new
                changed = True
                print(f'  patched: {old[:70]}')
                break
        src = '\n'.join(lines)
    elif new in src:
        print(f'  already patched: {new[:70]}')
    else:
        print(f'  WARNING: pattern not found: {old[:70]}')

if changed:
    path.with_suffix('.js.bak').write_text(path.read_text() if path.with_suffix('.js.bak').exists() else '')
    path.write_text(src)
    print(f'  saved: {path}')
PYEOF

# ---------------------------------------------------------------------------
# 2. license-state.js — аналогичные патчи для @n8n/backend-common
# ---------------------------------------------------------------------------
LICENSE_STATE_JS="$N8N_DIR/node_modules/@n8n/backend-common/dist/license-state.js"

if [ ! -f "$LICENSE_STATE_JS" ]; then
  echo "  WARNING: license-state.js not found at $LICENSE_STATE_JS, skipping"
else

python3 - "$LICENSE_STATE_JS" << 'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
src = path.read_text()

patches = [
    (
        "return this.isLicensed('feat:apiDisabled');",
        "return false;"
    ),
    (
        "return this.getValue('quota:maxTeamProjects') ?? 0;",
        "return constants_1.UNLIMITED_LICENSE_QUOTA;"
    ),
    (
        "return this.getValue('quota:aiCredits') ?? 0;",
        "return constants_1.UNLIMITED_LICENSE_QUOTA;"
    ),
    (
        "return this.getValue('quota:evaluations:maxWorkflows') ?? 0;",
        "return constants_1.UNLIMITED_LICENSE_QUOTA;"
    ),
    (
        "return this.getValue('quota:insights:maxHistoryDays') ?? 7;",
        "return 365;"
    ),
]

changed = False
for old, new in patches:
    if old in src:
        lines = src.split('\n')
        for i, line in enumerate(lines):
            if old in line:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = indent + new
                changed = True
                print(f'  patched: {old[:70]}')
                break
        src = '\n'.join(lines)
    elif new in src:
        print(f'  already patched: {new[:70]}')
    else:
        print(f'  WARNING: pattern not found: {old[:70]}')

if changed:
    path.write_text(src)
    print(f'  saved: {path}')
PYEOF

fi

# ---------------------------------------------------------------------------
# 3. WorkflowsView — router.resolve().href → .fullPath (фикс двойного /n8n/)
# ---------------------------------------------------------------------------
WORKFLOWS_VIEW=$(find "$N8N_DIR/node_modules/n8n-editor-ui/dist/assets" \
  -name 'WorkflowsView-*.js' ! -name '*-legacy-*' ! -name '*.bak' 2>/dev/null | head -1)

if [ -z "$WORKFLOWS_VIEW" ]; then
  echo "  WARNING: WorkflowsView-*.js not found, skipping"
else
  python3 - "$WORKFLOWS_VIEW" << 'PYEOF'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
src = path.read_text()

count_semi = src.count('}).href;')

if count_semi == 0:
    if '}).fullPath;' in src:
        print(f'  already patched: {path.name}')
    else:
        print(f'  WARNING: pattern ]).href; not found in ' + path.name)
    sys.exit(0)

new_src = src.replace('}).href;', '}).fullPath;')
path.with_suffix('.js.bak').write_text(src)
path.write_text(new_src)
print('  patched ' + str(count_semi) + ' occurrences of .href -> .fullPath in ' + path.name)
PYEOF
fi

echo "==> patch-n8n.sh done"
