# KG7QIN PR Integration Log

Branch: `integration/kg7qin`  
Base: `origin/dev`

## Policy Constraints
- Exclude `!plugins` command from all integrations.
- Treat command behaviors that create multi-message output as regressions.
- Keep `config.ini.example` and related config templates brief; move long-form docs to `docs/`.

## Inventory and Initial Classification

### Stage 1
- PR #138
  - `accept`: `aa94d23`, `69675ac`, `0be6004`, `6a627c2`, `975d744`, `0cbd764`
  - `accept-with-edit`: none
  - `drop`: lint-only/shared-sync commits already covered elsewhere
- PR #140
  - `accept`: `38d040a`, `04eba0a`
  - `accept-with-edit`: none
  - `drop`: broad lint/mypy sweep commit (`7af161e`) unless needed by gate
- PR #147
  - `accept`: `df66761`
  - `accept-with-edit`: `ca67ec4` (only if required to restore expected flood-scope/public-channel behavior)
  - `drop`: test/lint hygiene commits unless required by gate
- PR #145
  - `accept`: `2272b86`
  - `accept-with-edit`: none
  - `drop`: broad ruff/sync commits unless required by gate
- PR #155
  - `accept`: `2b896c6` (delta-only post-rebase compatibility)
  - `drop`: duplicated #138 history
- PR #156
  - `accept`: `5ac7ae0` (delta-only post-rebase compatibility)
  - `drop`: duplicated #147 history

### Stage 2
- PR #139
  - `accept`: `7450ac3`
  - `accept-with-edit`: `86264d6` docs portions only if concise for config templates
  - `drop`: duplicated lint/sync commits
- PR #149
  - `accept`: `88e8fa4`, `f6e1924`
  - `accept-with-edit`: none
  - `drop`: duplicated lint/sync commits unless required by gate
- PR #148
  - `accept`: `4a96f7f` (partial: `--show-config`, `--show-config-json`, `/admin/config`)
  - `accept-with-edit`: `655da24` (keep `!status`, drop `!plugins`, reject any multi-message command behavior)
  - `drop`: `9310a38`, `4dd9834` if they are mostly plugins-focused
- PR #154
  - `accept`: `a02c15f`
  - `accept-with-edit`: none
  - `drop`: none initially
- PR #141
  - `accept-with-edit`: `a8edb80` (preserve single-message behavior)
  - `drop`: duplicated lint/sync commits unless required
- PR #157
  - `accept`: `6eb8001` if needed after #148 partial integration
  - `drop`: duplicated #148 history
- PR #158
  - `accept`: `b2dddc5` if needed after #149 integration
  - `drop`: duplicated #149 history

### Stage 3
- PR #142
  - `accept-with-edit`: `a85e6ac`, `f4df680`, `9683abb`, `86b2f53`, `21eed98` (only tests/coverage updates that still reflect current code and policy)
  - `drop`: duplicated lint/sync commits
- PR #159
  - `accept`: `6302b07` (targeted ruff/mypy compliance)

## Execution Records
- Pending implementation.
