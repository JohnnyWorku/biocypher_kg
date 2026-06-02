# BioCypher Knowledge Graph CLI Tool

A user-friendly command line interface for generating knowledge graphs using BioCypher, with support for both Human and Drosophila melanogaster (Fly) data.

## Features

- 🧬 Human and 🪰 Fly organism support  
- ⚡ Default configurations for quick start  
- 🛠️ Custom configuration options  
- 📊 Interactive menu system with rich visual interface  
- 🔍 Multiple output formats (Neo4j, MeTTa, Prolog)  
- 📈 Progress tracking and logging  

## Installation

### Prerequisites

- Python 3.9+  
- Poetry (for dependency management)  

### Setup

```bash
# 1. Clone the repository:
git clone https://github.com/rejuve-bio/biocypher-kg.git
cd biocypher-kg

# 2. Install dependencies using Poetry
poetry install

# 3. Create required directories and run the CLI
mkdir -p output_human output_fly
poetry run python biocypher_cli/cli.py

# 📂 Project Structure:
# biocypher-kg/
# ├── biocypher_cli/            # CLI source code
# │   └── cli.py
# ├── config/                   # Configuration files or (Custom Config files)
# │   ├── adapters_config.yaml/adapters_config_sample.yaml
# │   ├── dmel_adapters_config.yaml/dmel_adapters_config_sample.yaml
# │   └── biocypher_config.yaml
# ├── aux_files/                # Auxiliary data files (or Custom config files)
# │   ├── gene_mapping.pkl/abc_tissues_to_ontology_map.pkl
# │   └── sample_dbsnp_rsids.pkl
# ├── output_human/             # Default human output
# ├── output_fly/               # Default fly output
# └── pyproject.toml            # Dependencies
```

```bash
cd /biocypher-kg
python biocypher_cli/cli.py
```
