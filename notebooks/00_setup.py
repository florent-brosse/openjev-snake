# Databricks notebook source
# MAGIC %md
# MAGIC # Delta Serpents — setup
# MAGIC Creates the schema and the three Delta tables written by the app (`decisions`, `events`, `games`)
# MAGIC and grants the app's service principal the rights to write them.

# COMMAND ----------

dbutils.widgets.text("catalog", "")  # required: your Unity Catalog catalog
dbutils.widgets.text("schema", "openjev_demo")
dbutils.widgets.text("app_sp", "")

import os
import re
import sys

catalog, schema, app_sp = (dbutils.widgets.get(k).strip() for k in ("catalog", "schema", "app_sp"))
assert catalog, "Set the `catalog` widget (or job parameter)."
for name in (catalog, schema):
    assert re.fullmatch(r"[A-Za-z0-9_]+", name), f"invalid identifier: {name}"

sys.path.append(os.path.abspath("../app"))
from decision_log import ddl  # same DDL the app uses

prefix = f"{catalog}.{schema}"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {prefix} COMMENT 'Delta Serpents: OpenJev decisions, events and games'")
for stmt in ddl(prefix):
    spark.sql(stmt)
print("tables:", [r.tableName for r in spark.sql(f"SHOW TABLES IN {prefix}").collect()])

# COMMAND ----------

if app_sp:
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", app_sp), "app_sp must be the app service principal application id"
    spark.sql(f"GRANT USE CATALOG ON CATALOG {catalog} TO `{app_sp}`")
    spark.sql(f"GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA {prefix} TO `{app_sp}`")
    print(f"granted write access on {prefix} to {app_sp}")
else:
    print("app_sp not provided: skipping grants")
