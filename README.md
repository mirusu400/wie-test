# wie-compat

Bulk compatibility tester for [wie](https://github.com/mirusu400/wie) (WIPI / J2ME emulator).

Downloads a corpus of Korean feature-phone games from archive.org, runs each
through `wie_cli`, and produces a compatibility report (JSON + Markdown).

## Quick start

```bash
pip install -r requirements.txt

# 1. Build wie_cli (once)
cargo build --release --manifest-path ../wie/Cargo.toml

# 2. Download + extract corpus (~1.1 GB zip)
python run_compat.py fetch

# 3. Run all games through the emulator
python run_compat.py test --wie ../wie/target/release/wie_cli.exe --timeout 25

# 4. Render report
python run_compat.py report
```

## Layout

```
corpus/        # extracted game files (gitignored)
results/       # per-game JSON results (gitignored, but committed report.md is)
report.md      # rendered compatibility table
report.json    # aggregated results
```

## How it classifies

| Status | Meaning |
|--------|---------|
| `ok_alive` | Process still running at timeout (likely booted) |
| `ok_exit` | Clean exit (game closed itself) |
| `unsupported_format` | wie says "Unknown archive format" / "Unknown file format" |
| `unimplemented` | Hit `unimplemented!()` / `Not yet implemented` / `todo!` |
| `panic` | Rust panic (other) |
| `load_error` | Errored before main loop |
| `runner_error` | Python-side spawn / decode failure |

The corpus archive is **not** redistributed by this repo. We only fetch it on
demand; the games themselves remain at the original archive.org item.

Source: https://archive.org/details/dubigame.tistory.com_mirror_202403
