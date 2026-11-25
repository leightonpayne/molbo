---
icon: lucide/file
---

# Inputs

`molbo` accepts local structure files, remote URLs, PDB IDs, and AlphaFold DB IDs.

## Local files

Supported local structure formats (each also supported with `.gz` compression):

- Macromolecular: `.pdb`, `.ent`, `.cif`, `.mmcif`, `.bcif`
- Small-molecule & docking: `.sdf`, `.sd`, `.mol`, `.mol2`, `.pdbqt`
- Coordinates & MD: `.xyz`, `.gro`

## Standard input (`-`)

Pipe a structure directly over `stdin` with `-` and `--format` (gzip-compressed streams are detected automatically):

```bash
cat structure.cif | molbo - --format cif
```

## Remote URLs

You can point `molbo` at an `http` or `https` URL and it will proxy the structure through the local session.

```bash
molbo https://files.rcsb.org/download/8U7I.cif
```

```bash
molbo https://alphafold.ebi.ac.uk/files/AF-0000000066327688-model_v1.bcif
```

If the remote URL does not end in a recognizable structure suffix, provide the format explicitly. For example, when repository APIs serve a protein structure behind a generic `/content` endpoint:

```bash
molbo 'https://zenodo.org/api/records/7739038/files/AF-PP2AA_B55alpha_PP2AC_FAM122A.cif/content' --format cif
```

## PDB IDs

`molbo` resolves standard 4-character PDB IDs (starting with a digit) and extended 12-character wwPDB IDs (`pdb_XXXXXXXX`) to RCSB downloads automatically:

```bash
molbo 1crn
```

```bash
molbo pdb_00008qbk
```

Pass `--format` to download a different representation from RCSB (`cif`/`mmcif`, `pdb`, or `bcif`):

```bash
molbo 1crn --format bcif
```

## AlphaFold DB IDs

Open AlphaFold DB structures by UniProt accession (`af:<accession>` / `alphafold:<accession>`) or AlphaFold entry ID (`AF-<accession>-F1`, optionally with `-model_v<n>`):

```bash
molbo af:P69905
```

```bash
molbo AF-P69905-F1 --format bcif
```
