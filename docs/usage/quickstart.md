---
icon: lucide/circle-power
---

# Quickstart

`molbo` opens a molecular structure in a local Mol* session and serves it from a small HTTP server.

## Basic examples

Open by PDB ID

```bash
molbo 9QXS
```

Open a local file:

```bash
molbo structure.pdb
```

Open a remote structure URL:

```bash
molbo https://alphafold.ebi.ac.uk/files/AF-0000000066236528-model_v1.bcif
```

Bind to a specific port:

```bash
molbo 9QXS --port 8080
```
Do not open the browser automatically:

```bash
molbo 9QXS --no-open
```

Auto-close when the browser tab goes away:

```bash
molbo 9QXS --auto-close
```