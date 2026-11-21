"""Shared offline fixtures for the migration schema checks; no database access.

The frozen-hash baseline is re-established after the X module (V4/V5/V15/V16) and the
GPT direct-charge module (V18/V21/V23/V24) were retired together with their tables, and
after V1/V19/V20 dropped those DDL statements. Only the surviving scripts are frozen.
"""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrate import expected_schema  # noqa: E402


# sha256 of every migration that ships in this release, at the moment the modules retired.
FROZEN_SHA256 = {
    "V1__mysql_schema.sql": "79d52a7fbf06803bf799fe33f2ca17458123ff4ab4b9f0b3be2b0e3b76ca3188",
    "V2__invitation_module.sql": "843580221ddd55a77922a9b08f23c84988ecd5f95323b819d5ad7ae03d381970",
    "V3__invitation_legacy_import.sql": "71a2def1e995a762fc0b8a49a6eb3a8915e2af03d534fb68ec609a877fadf4fa",
    "V6__invitation_flow_completion.sql": "40081511cb82187e367ff0b2088f7dbc1241eb1bf6ebebb4bbafe09b0bd80504",
    "V7__python_invitation.sql": "d483e9d1e89e3aa79356a26d98f86b34b5a77591c854487a7f288106ead5e8d8",
    "V8__invitation_execution_permits.sql": "99171e6509fb6d0caeadcf40ebc6a5fba37c8c93d33bebb9b1d2de913afd8143",
    "V9__invitation_sms_and_manual_completion.sql": "3320ccbffdc4c49a2367eef222d34e45ddc97bad4282df06134b787dc9ade540",
    "V10__invitation_redemption_attempts.sql": "f3d70ab6271de9accfdf0e9b26aab2a74f2c69ece44d485398d01a3f75771869",
    "V11__proxy_dual_409_locks.sql": "ff2dee324ec7b4c4558f7c7ac25e838f9be45a8cc65b4f6909b7221b6eb106c7",
    "V12__invitation_run_stage.sql": "cfad130869429c7a0ccefccececeab0599af15cf6ce4abc7e6c84b8609a8c463",
    "V13__account_phone_binding.sql": "37ce6b7766b5de05f422156563fa5192c2efb62a2f738d1b5c5c79c3d732a7fb",
    "V14__account_used_proxy.sql": "d87729660be06b719276ef7f533662f3ffeee654967897e99112fdc0525fe498",
    "V17__invitation_task_hold_review.sql": "00f16fa259942caaa95b30d9cf7f2bd830492ed280c6a50a0b61a45e4a9745bd",
    "V19__stats_cost_fields.sql": "726cadca6d7f159c1355951b7c47a3762a4287300464f43c2a4460bcb804f11c",
    "V20__integrity_constraints.sql": "1affc3c4d6b7a694743af8ee215358f9bc73ed7b4b0aa1f1fd5a950eac1e4600",
    "V22__admin_ip_bans.sql": "dfbee38ad8a64e57e9ff7eb401cd522172418d9b7a59b38bf31957dc569be405",
    "V25__sms_voip_area_exclusions.sql": "d276e4cf772d17eec592ea5286110efc9f69cbb6c989214c39be45a497845d73",
    "V26__sms_area_exclusion_active.sql": "676cca789e78b474cef55050ca5c6dcbae7b304265849e7a286bbc602c6b6538",
}


class SchemaMetadata:
    """Information-schema fixture so the real maintenance validator can run offline."""

    def __init__(self, items, *, missing_unique=None):
        self.schema = expected_schema(items)
        self.missing_unique = set(missing_unique or ())

    def all(self, sql, *params):
        if 'COLUMN_NAME,DATA_TYPE' in sql:
            aliases = {'boolean': 'tinyint', 'integer': 'int', 'numeric': 'decimal'}
            return [{'TABLE_NAME': table, 'COLUMN_NAME': name,
                     'DATA_TYPE': aliases.get(spec['type'], spec['type']),
                     'COLUMN_TYPE': spec['type'],
                     'IS_NULLABLE': 'NO' if spec['not_null'] else 'YES',
                     'CHARACTER_MAXIMUM_LENGTH': int(spec['size']) if spec['type'] in {'char', 'varchar'} else None}
                    for table, columns in self.schema.items() for name, spec in columns.items()]
        if 'SELECT slot_id' in sql:
            return [{'slot_id': slot_id} for slot_id in range(33)]
        raise AssertionError(sql)

    def one(self, sql, *params):
        if 'INDEX_NAME' in sql and 'NON_UNIQUE=0' in sql:
            table, name = params
            return None if (table, name) in self.missing_unique else {'INDEX_NAME': name}
        raise AssertionError(sql)
