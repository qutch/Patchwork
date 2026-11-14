"""Scripted model turns for the offline DeepAgentService tests (fixture recording + Break 1)."""

from __future__ import annotations

from tests.agents.fake_model import call, turn

GLOBEX = {"id": "globex_v1", "match": {"sender_domain": "globex.com"},
          "fields": {"po_id": {"regex": r"purchase order\s+(PO-\d+)"},
                     "sku": {"regex": r"units of item\s+([A-Z0-9-]+)"},
                     "qty": {"regex": r"receive\s+(\d+)\s+units", "cast": "int"}}}
INITECH = {"id": "initech_v1", "match": {"sender_domain": "initech.com"},
           "fields": {"po_id": {"regex": r"REF=(PO-\d+)"},
                      "sku": {"regex": r"PART=([A-Z0-9-]+)"},
                      "qty": {"regex": r"UNITS=(\d+)", "cast": "int"}}}

TODOS = [{"content": "Interpret the recording", "status": "in_progress"},
         {"content": "Compile and test", "status": "pending"},
         {"content": "Dry run on staging", "status": "pending"},
         {"content": "Request deploy", "status": "pending"}]


def build_script() -> list:
    return [
        # supervisor
        turn(call("write_todos", todos=TODOS), text="Planning the build."),
        turn(call("task", subagent_type="interpreter", description="Recording fixture -> spec.yaml for po_to_stockroom")),
        # interpreter
        turn(call("get_recording_summary"), call("get_candidates")),
        turn(call("draft_spec", field_names={"product_id": "sku", "quantity": "qty", "reference": "po_id"})),
        turn(call("fetch_source_samples", limit=15)),
        turn(call("test_parser")),
        turn(call("test_parser", parser=GLOBEX), call("test_parser", parser=INITECH)),
        turn(call("propose_spec_patch", patch=[{"op": "add", "path": "/extract/parsers/-", "value": GLOBEX},
                                               {"op": "add", "path": "/extract/parsers/-", "value": INITECH}],
                  reason="two more supplier formats")),
        turn(call("add_test_case", record_id="msg-0001",
                  expect_body={"product_id": "WID-114", "quantity": 34, "reference": "PO-2033"})),
        turn(call("ask_user", question="Some messages are not orders (e.g. holiday notices). What should happen to them?",
                  options=["Quarantine them for review", "Skip them silently"])),
        turn(call("validate_spec")),
        turn(text="Spec has 3 parsers (acme, globex, initech) extracting po_id, sku, qty."),
        # supervisor
        turn(call("write_todos", todos=[{**TODOS[0], "status": "completed"}, {**TODOS[1], "status": "in_progress"}, *TODOS[2:]])),
        turn(call("task", subagent_type="builder", description="Compile spec.yaml and run tests")),
        # builder
        turn(call("compile_spec")),
        turn(text="v1 compiled; tests passed."),
        # supervisor
        turn(call("task", subagent_type="tester", description="Dry run the passing version")),
        # tester
        turn(call("dry_run", limit=5)),
        turn(text="Dry run completed on staging."),
        # supervisor
        turn(call("request_deploy", version=1, summary="Initial version built from the recording.")),
        turn(text="v1 is deployed."),
    ]


def repair_script(answer_by_supplier: bool = True) -> list:
    warehouse = ({"transform": "lookup", "from": "record.sender_domain",
                  "args": {"table": {"acme.com": "BOS", "globex.com": "NYC", "initech.com": "BOS"}}}
                 if answer_by_supplier else {"const": "BOS"})
    patch = [{"op": "remove", "path": "/sink/body/product_id"},
             {"op": "remove", "path": "/sink/body/quantity"},
             {"op": "add", "path": "/sink/body/sku", "value": {"from": "sku"}},
             {"op": "add", "path": "/sink/body/quantity_delta", "value": {"from": "qty"}},
             {"op": "add", "path": "/sink/body/warehouse_id", "value": warehouse}]
    return [
        turn(call("write_todos", todos=[{"content": "Diagnose the incident", "status": "in_progress"},
                                        {"content": "Patch the spec", "status": "pending"},
                                        {"content": "Compile, test, request deploy", "status": "pending"}])),
        turn(call("task", subagent_type="repairer", description="Repair incident")),
        # repairer
        turn(call("get_incident")),
        turn(call("diff_openapi")),
        turn(call("ask_user", question="Stockroom now requires warehouse_id. Always BOS, or by supplier?",
                  options=["Always BOS", "By supplier (tell me which warehouse for each supplier)"])),
        turn(call("propose_spec_patch", patch=patch, reason="Stockroom contract v2")),
        turn(call("migrate_golden_examples")),
        turn(text="Patched 2 renames and the warehouse_id rule."),
        # supervisor
        turn(call("task", subagent_type="builder", description="Compile the patched spec")),
        turn(call("compile_spec")),
        turn(text="v2 compiled; tests passed."),
        turn(call("request_deploy", version=2, summary="Adapt to Stockroom contract v2.")),
        turn(text="v2 deployed."),
    ]


# ---- v2: LEARN (careers.airbnb.com, a branded Greenhouse page) -----------------------------------

AIRBNB = {"id": "airbnb", "fields": {"title": {"css": "h1"},
                                     "company": {"css": 'meta[property="og:site_name"]', "attr": "content",
                                                 "regex": "^Careers at (.+)$"}}}
LEARN_TODOS = [{"content": "Read the held page", "status": "in_progress"},
               {"content": "Write and test a parser", "status": "pending"},
               {"content": "Save it (code decides on deploy)", "status": "pending"}]


def learn_script(page_id: str = "pg-000005", parser: dict | None = None) -> list:
    parser = parser or AIRBNB
    return [
        # supervisor
        turn(call("write_todos", todos=LEARN_TODOS)),
        turn(call("task", subagent_type="learner",
                  description=f"Learn page {page_id} (site careers.airbnb.com) for jobs_to_tracker")),
        # learner
        turn(call("get_page")),
        turn(call("test_page_parser", parser=parser)),
        turn(call("save_parser", parser=parser, reason="branded Greenhouse careers site")),
        turn(text="Saved parser airbnb (title from h1, company from og:site_name); deployed automatically."),
        # supervisor
        turn(text="Learned careers.airbnb.com: parser airbnb, deployed automatically after every check passed."),
    ]
