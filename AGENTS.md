# AGENTS.md — Omni-Organizer Engineering & Safety Redlines

This file defines mandatory safety and engineering rules for any AI assistant or automation script modifying this repository.

---

## 1. Configuration Separation (Public Templates vs Local State)

- Git-tracked public templates (`config/v3_settings.example.json`, `config/config.example.yaml`, `config/monitored_paths.example.json`) MUST remain generic and free of personal paths, usernames, or secrets.
- Personal configs (`config/v3_settings.local.json`, `config/config.yaml`, `config/monitored_paths.json`, `config/bridge_secret.local.txt`) and runtime directories (`state/`, `logs/`, `reports/*`) are gitignored and MUST NEVER be committed.
- If `config/config.yaml` is maintained via NTFS HardLink on Windows, validate before any live maintenance change without breaking the link:
  ```powershell
  python -m organize check config\config.yaml
  python -m organize sim config\config.yaml
  ```

---

## 2. Non-Negotiable Safety Redlines

1. **Never move or mutate installed games, SDKs, engines, or OS directories**:
   - System paths (`C:\Windows`, `C:\Program Files`, etc.) and user-declared `protected_paths` (games, engines, encrypted vaults) must never be relocated.
2. **Never break code repositories or virtual environments**:
   - Guardrails must reject files inside directories containing `.git`, `package.json`, `pyproject.toml`, `.venv`, `venv`, `Cargo.toml`, `go.mod`, or `CMakeLists.txt`.
3. **Never read, log, or send sensitive files to any LLM**:
   - Files matching sensitive filename patterns (`passwd`, `secret`, `.env`, `.pem`, `.key`, IDs, contracts, bank statements) or containing PII/key patterns in text previews must bypass AI inference completely and route only to human review.
4. **Never commit secrets or tokens**:
   - Bridge Bearer tokens (`bridge_secret.local.txt`), API keys, and personal file inventories must remain untracked.
5. **Reversibility & Recycle Bin first**:
   - File moves must use `on_conflict: skip` and record transactions in `state/undo_journal.jsonl`. Cleanup sweeps must use `trash` (Recycle Bin) rather than irreversible `delete` on real user files.
6. **Clean daemon lifecycle**:
   - Background `watcher.py` launches on Windows must go through `src/manage_service.py` (Task Scheduler detachment) so parent shell exit does not terminate the watcher.
