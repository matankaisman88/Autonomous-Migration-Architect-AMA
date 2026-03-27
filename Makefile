.PHONY: help prepare-dirs \
	chaos-generate-sqlserver chaos-generate-oracle chaos-generate-db2 chaos-generate-all \
	chaos-report-sqlserver chaos-report-oracle chaos-report-db2 chaos-report-all \
	demo-sqlserver demo-oracle demo-db2 demo-multi-source demo-extreme-chaos

PYTHON ?= python
AMA_CLI ?= -m ama.cli
LINES ?= 200000
SCALE ?= 1000
CHAOS_DIR ?= chaos_data
REPORT_DIR ?= sample_data/generated_chaos

help:
	@echo "AMA demo targets"
	@echo "  make demo-sqlserver      # generate + ingest SQL Server chaos dataset"
	@echo "  make demo-oracle         # generate + ingest Oracle chaos dataset"
	@echo "  make demo-db2            # generate + ingest DB2 chaos dataset"
	@echo "  make demo-multi-source   # generate + ingest all three dialects"
	@echo "  make demo-extreme-chaos  # larger SQL Server stress dataset (1M rows)"
	@echo ""
	@echo "Configurable vars: LINES=$(LINES) SCALE=$(SCALE) PYTHON=$(PYTHON)"

prepare-dirs:
	@mkdir -p $(CHAOS_DIR)/sql_logs $(CHAOS_DIR)/ddl $(REPORT_DIR)

chaos-generate-sqlserver: prepare-dirs
	$(PYTHON) tools/generate_extreme_chaos.py --source-dialect sqlserver --scale $(SCALE) --lines $(LINES) --out $(CHAOS_DIR)/sql_logs/extreme_sqlserver.jsonl --ddl-out $(CHAOS_DIR)/ddl/extreme_sqlserver_ddl.sql --manifest-out $(CHAOS_DIR)/ddl/extreme_sqlserver_manifest.json

chaos-generate-oracle: prepare-dirs
	$(PYTHON) tools/generate_extreme_chaos.py --source-dialect oracle --scale $(SCALE) --lines $(LINES) --out $(CHAOS_DIR)/sql_logs/extreme_oracle.jsonl --ddl-out $(CHAOS_DIR)/ddl/extreme_oracle_ddl.sql --manifest-out $(CHAOS_DIR)/ddl/extreme_oracle_manifest.json

chaos-generate-db2: prepare-dirs
	$(PYTHON) tools/generate_extreme_chaos.py --source-dialect db2 --scale $(SCALE) --lines $(LINES) --out $(CHAOS_DIR)/sql_logs/extreme_db2.jsonl --ddl-out $(CHAOS_DIR)/ddl/extreme_db2_ddl.sql --manifest-out $(CHAOS_DIR)/ddl/extreme_db2_manifest.json

chaos-generate-all: chaos-generate-sqlserver chaos-generate-oracle chaos-generate-db2

chaos-report-sqlserver: prepare-dirs
	$(PYTHON) $(AMA_CLI) run --sql-logs $(CHAOS_DIR)/sql_logs/extreme_sqlserver.jsonl --discovery-mode --no-target --no-ddl-merge --format json --out-file $(REPORT_DIR)/sqlserver_report.json

chaos-report-oracle: prepare-dirs
	$(PYTHON) $(AMA_CLI) run --sql-logs $(CHAOS_DIR)/sql_logs/extreme_oracle.jsonl --discovery-mode --no-target --no-ddl-merge --format json --out-file $(REPORT_DIR)/oracle_report.json

chaos-report-db2: prepare-dirs
	$(PYTHON) $(AMA_CLI) run --sql-logs $(CHAOS_DIR)/sql_logs/extreme_db2.jsonl --discovery-mode --no-target --no-ddl-merge --format json --out-file $(REPORT_DIR)/db2_report.json

chaos-report-all: chaos-report-sqlserver chaos-report-oracle chaos-report-db2

demo-sqlserver: chaos-generate-sqlserver chaos-report-sqlserver

demo-oracle: chaos-generate-oracle chaos-report-oracle

demo-db2: chaos-generate-db2 chaos-report-db2

demo-multi-source: chaos-generate-all chaos-report-all

demo-extreme-chaos: prepare-dirs
	$(PYTHON) tools/generate_extreme_chaos.py --source-dialect sqlserver --scale 1000 --lines 1000000 --out $(CHAOS_DIR)/sql_logs/extreme_1m_sqlserver.jsonl --ddl-out $(CHAOS_DIR)/ddl/extreme_1m_sqlserver_ddl.sql --manifest-out $(CHAOS_DIR)/ddl/extreme_1m_sqlserver_manifest.json
	$(PYTHON) $(AMA_CLI) run --sql-logs $(CHAOS_DIR)/sql_logs/extreme_1m_sqlserver.jsonl --discovery-mode --no-target --no-ddl-merge --format json --out-file $(REPORT_DIR)/extreme_1m_sqlserver_report.json
