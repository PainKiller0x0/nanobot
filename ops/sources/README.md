# Source snapshots

Runtime source snapshots for Rust sidecars on the live server.

Included:
- _shared/: shared Python helpers for ops skill clients
- Cargo manifests and lockfiles
- selected files from `src/`
- lightweight build files such as Dockerfiles and README files

Excluded on purpose:
- `.env`
- databases
- logs
- `target/`
- runtime `data/`
- live task configs that may contain local-only commands
