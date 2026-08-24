# Maverick Pro — Local Agent Edition

This keeps Maverick as a normal HTML app. A small Python bridge runs in a separate terminal and lets the HTML invoke a local terminal agent without putting an OpenAI/Anthropic API key in the browser.

## Files

- `Maverick_Pro_Agent.html` — Maverick UI with Local AI Underwriter controls.
- `bridge.py` — loopback-only HTTP bridge and session manager. Uses only the Python standard library.
- `start_maverick.bat` — Windows launcher for Codex CLI.
- `start_maverick_claude.bat` — Windows launcher for Claude CLI.
- `start_maverick_mock.bat` — test the integration without any AI agent.
- `start_maverick.sh` — macOS/Linux launcher (Codex by default).

The bridge creates `sessions/` and `results/` beside itself automatically.

## Codex setup

1. Install the Codex CLI and sign in with your ChatGPT account using the CLI's current sign-in flow.
2. Confirm `codex` works in a normal terminal.
3. Put all files in the same folder.
4. Double-click `start_maverick.bat` (or run `python bridge.py`).
5. Keep the terminal open.
6. Open `http://127.0.0.1:8765`.
7. In Maverick, Fetch Data as usual, then click **Underwrite batch**.

By default the bridge runs:

`codex exec --skip-git-repo-check --sandbox read-only -`

The underwriting prompt is sent through stdin. If your Codex CLI version uses different non-interactive flags, set an override before starting:

`set MAVERICK_AGENT_CMD=your command here`

Then run `python bridge.py`.

## Claude setup

Set `MAVERICK_AGENT=claude` or use `start_maverick_claude.bat`. The default is:

`claude -p --output-format text`

You can also override it with `MAVERICK_AGENT_CMD`.

## Test without AI

Run `start_maverick_mock.bat`. The bridge returns WATCH for every candidate and lets you verify batching, session persistence, final JSON generation, UI rendering, and downloads without consuming any agent usage.

## How batching works

- Maverick still applies its deterministic scanner first.
- Every local PASS stock remains eligible.
- Stocks are sent to the agent in the existing small batches (default 4).
- Each batch returns strict structured JSON.
- The bridge saves each batch under `sessions/<session-id>.json`.
- On the final batch, the bridge automatically combines all prior batch verdicts and runs the global finalist comparison.
- The final normalized JSON is saved under `results/<session-id>_final.json` and appears in Maverick's **LLM Cleared / Finalists** section.

## Important external-check limitation

The bridge does not claim that a terminal agent has live web access. The prompt explicitly requires UNKNOWN / NOT SUPPLIED and caps a stock at WATCH when a required live check cannot actually be verified. If your chosen agent has a permitted browser/web tool, it may use it; otherwise it must not silently pass event, F&O-ban, MWPL, sector-RS, or similar live checks.

## Security

- The server binds to `127.0.0.1` by default, so it is not exposed to your LAN or the public internet.
- The bridge never receives an OpenAI or Anthropic API key from the HTML.
- Do not change `MAVERICK_HOST` to `0.0.0.0` unless you understand the network/security implications.
