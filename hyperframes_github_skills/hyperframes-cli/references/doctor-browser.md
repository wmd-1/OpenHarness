# doctor, browser

Environment diagnosis and bundled-Chrome management. Run these first when a render or preview fails.

> **⚠ OpenHarness runtime note** — Chrome is **already configured for you** in the OpenHarness Docker runtime: `PRODUCER_HEADLESS_SHELL_PATH` and `CHROME_HEADLESS_BIN` are both pre-set to `/opt/chrome-headless-shell-linux64/chrome-headless-shell` (injected by `service/app/workers/runner.py` and `docker-compose.yml`). **Do not set the Chrome path yourself, do not run `browser ensure`, and do not pass `--browser-path` to `render`. Just run `npx hyperframes render`.** Only read the rest of this file if `render` actually fails with a Chrome error.

## Using a specific Chrome for `render`

`render` does **not** accept `--browser-path` — that flag is `preview`/`play` only (see `preview-render.md`). To point `render` at a specific Chrome / chrome-headless-shell binary, set the **`PRODUCER_HEADLESS_SHELL_PATH`** environment variable:

```bash
PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell \
  npx hyperframes render --quality draft --output out.mp4
```

- `npx hyperframes browser ensure` downloads the **pinned bundled** Chrome (for reproducible pixel output across machines) — it does **not** adopt an existing binary, so it is the wrong tool when a Chrome path is already supplied by the environment.
- `--browser-path` / `--user-data-dir` / `--remote-debugging-port` are `preview`/`play` flags and are ignored by `render`.

## doctor

```bash
npx hyperframes doctor
npx hyperframes doctor --json     # CI / agent output (always exit 0; gate on payload `ok`)
```

Runs independent checks and reports each as ok/warn/fail:

- **Version** — installed CLI vs latest on npm (hints upgrade when stale)
- **Node.js** — ≥ 22 required
- **CPU**, **Memory**, **Disk** — host resources
- **Environment** — env vars that affect the renderer
- **FFmpeg** / **FFprobe** — found, version, codecs
- **Chrome** — bundled or system, version, path
- **Docker** / **Docker running** — required only for `render --docker`
- **/dev/shm** — inside containers only

Run `doctor` first when:

- `render` fails with a Chrome or FFmpeg error.
- `preview` opens but the composition fails to load.
- A fresh machine has never run HyperFrames.

Common issues:

- **Missing FFmpeg** — install via `brew install ffmpeg` (macOS) or your package manager.
- **Missing bundled Chrome** — run `npx hyperframes browser ensure`. **Caveat:** doctor's `Chrome` check only inspects the **bundled** build — it does **not** read `PRODUCER_HEADLESS_SHELL_PATH`. If you point `render` at a binary via that env var, doctor will still report Chrome as "not found"; that is **expected**. Gate on whether `render` actually succeeds, not on doctor's Chrome line.
- **Low memory** — close other Chromes, reduce `--workers`, or use `--quality draft`.

## browser

```bash
npx hyperframes browser ensure    # find or download the pinned Chrome
npx hyperframes browser path      # print the browser executable path (for scripting)
npx hyperframes browser clear     # remove the cached Chrome download
```

Manage the Chrome build HyperFrames uses for rendering. The pinned version exists because pixel output drifts across Chrome versions — using the bundled build keeps rendered output reproducible across machines.

Use `path` to embed the binary in scripts: `$(npx hyperframes browser path)`.
