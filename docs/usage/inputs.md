---
icon: lucide/file
---

# Inputs

`molbo` accepts three kinds of inputs.

## Local files

Supported local structure formats:

- `.pdb`
- `.cif`
- `.mmcif`
- `.bcif`
- `.pdb.gz`
- `.cif.gz`
- `.mmcif.gz`
- `.bcif.gz`

## Remote URLs

You can point `molbo` at an `http` or `https` URL and it will proxy the structure through the local session.

```bash
molbo https://files.rcsb.org/download/8U7I.cif
```

```bash
molbo https://alphafold.ebi.ac.uk/files/AF-0000000066327688-model_v1.bcif
```

If the remote URL does not end in a recognizable structure suffix, provide the format explicitly. For example, when repository APIs serve a protein structure behind a generic `/content` endpoint:

    molbo 'https://zenodo.org/api/records/7739038/files/AF-PP2AA_B55alpha_PP2AC_FAM122A.cif/content' --format cif

## PDB IDs

If the input is a 4-character PDB ID, `molbo` resolves it to an RCSB mmCIF download automatically.

```bash
molbo 1crn
```

```bash
molbo 8qbk
```