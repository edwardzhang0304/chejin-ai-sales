# test02 Recorder Seed Package

This folder is a Git-tracked delivery package for **AI Smart Recorder** seed data.

## Why this exists

`runtime/apps/...` is ignored by `.gitignore`, so runtime migration files cannot be shared through GitHub directly.
This folder provides a stable, downloadable package in the repository.

## Files

- `test02_recorder_only_20260602_040502.zip` (latest)
- `install_seed.ps1`
- `操作指南.md` (human-friendly Chinese guide)
- `import_guide.json` (machine-readable guide for AI agents)

Package SHA256:

`E30BF60FB04E87D5098C9A8B3700E31CB302B3DF6C4D94AE4D5EC5FFFDD47618`

## Quick install on another machine

1. Clone/download repository.
2. Stop local admin backend and related worker processes.
3. Run (installer auto-picks the latest `test02_recorder_only_*.zip`):

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\wechat_ai_customer_service\deploy\recorder_seed\test02\install_seed.ps1 -WorkspaceRoot "D:\AI\omniauto"
```

4. Restart services.
5. Login and verify recorder data under tenant `test02`.

## Safety scope

- Imports only `payload/runtime/tenants/test02/*`.
- By default, also merges only `test02` tenant binding and key recorder module configs into global files (with backups).
- If you do not want global merge, run installer with `-ApplyGlobalRecorderModules:$false`.
- Includes optional reference material inside the zip under `optional_reference/`.
