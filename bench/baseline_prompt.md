# Baseline prompt

The exact prompt the one-shot baseline (`bench/baseline_script.py`) was written from. It was given, verbatim and in one shot, to a fresh Claude session that had no access to the repository, the seed, the ground truth or the Break 1 / Break 2 code. Its answer was saved unchanged except for the header comment.

---

I want a small Python script that does this job for me:

> For every new purchase order message, record incoming stock

Today I do it by hand: I open a purchase order message in our Supplier Inbox (http://localhost:8001), read the
PO number, product SKU and quantity, then go to our Stockroom app (http://localhost:8002), open
"Add incoming stock" and fill in Product, Quantity and Reference (the PO number). I recorded myself doing it once
in the browser; the recording is below. Both apps have JSON APIs, so the script doesn't need a browser.

We get purchase orders from three suppliers (Acme, Globex, Initech) and each writes them differently. Six recent
messages from the inbox API are below, plus the Acme one in the recording. Not every message in the inbox is a
purchase order.

Please write one Python script (requests or httpx is fine) that I can run from cron every few minutes. It should
pick up new purchase orders and record each one in the Stockroom once. Read the base URLs from the `INBOX_URL` and
`STOCKROOM_URL` environment variables, defaulting to the localhost ones above.

## Recording of me doing it once

```json
{
  "id": "fixture-po-001",
  "intent": "For every new purchase order message, record incoming stock",
  "allowlist": [
    "localhost:8001",
    "localhost:8002"
  ],
  "started_at": 1762070400000,
  "stopped_at": 1762070416500,
  "note": "Hand-made fixture (plan section 12). Mirrors what capture/recorder.js will emit; built from seeded message msg-0007.",
  "events": [
    {
      "t": 1762070400000,
      "type": "framenavigated",
      "url": "http://localhost:8001/",
      "title": "Supplier Inbox"
    },
    {
      "t": 1762070400120,
      "type": "net",
      "method": "GET",
      "url": "/api/messages",
      "status": 200,
      "req": null,
      "resp": [
        {
          "id": "msg-0007",
          "sender": "orders@acme.com",
          "sender_domain": "acme.com",
          "subject": "Purchase Order PO-1022",
          "body": "Hi team,\n\nPlease find our order below.\n\nPO Number: PO-1022\nSKU: GAD-217\nQty: 20\n\nThanks,\nAcme Supply",
          "received_at": "2026-11-02T10:32:33Z"
        }
      ]
    },
    {
      "t": 1762070402400,
      "type": "click",
      "url": "http://localhost:8001/",
      "el": {
        "tag": "a",
        "role": "link",
        "name": "Purchase Order PO-1022",
        "label": null,
        "testid": null,
        "css": "table tr td a",
        "nearby": "orders@acme.com"
      }
    },
    {
      "t": 1762070402600,
      "type": "framenavigated",
      "url": "http://localhost:8001/messages/msg-0007",
      "title": "Purchase Order PO-1022"
    },
    {
      "t": 1762070402650,
      "type": "net",
      "method": "GET",
      "url": "/api/messages/msg-0007",
      "status": 200,
      "req": null,
      "resp": {
        "id": "msg-0007",
        "sender": "orders@acme.com",
        "sender_domain": "acme.com",
        "subject": "Purchase Order PO-1022",
        "body": "Hi team,\n\nPlease find our order below.\n\nPO Number: PO-1022\nSKU: GAD-217\nQty: 20\n\nThanks,\nAcme Supply",
        "received_at": "2026-11-02T10:32:33Z"
      }
    },
    {
      "t": 1762070406100,
      "type": "copy",
      "url": "http://localhost:8001/messages/msg-0007",
      "text": "PO-1022",
      "el": {
        "tag": "pre",
        "role": null,
        "name": null,
        "label": null,
        "testid": "message-body",
        "css": "pre",
        "nearby": "PO Number:"
      }
    },
    {
      "t": 1762070409000,
      "type": "framenavigated",
      "url": "http://localhost:8002/incoming/new",
      "title": "Add incoming stock"
    },
    {
      "t": 1762070411200,
      "type": "change",
      "url": "http://localhost:8002/incoming/new",
      "value": "GAD-217",
      "el": {
        "tag": "select",
        "role": "combobox",
        "name": "product_id",
        "label": "Product",
        "testid": "product",
        "css": "form#f select",
        "nearby": "Product"
      }
    },
    {
      "t": 1762070413050,
      "type": "input",
      "url": "http://localhost:8002/incoming/new",
      "value": "20",
      "el": {
        "tag": "input",
        "role": "spinbutton",
        "name": "quantity",
        "label": "Quantity",
        "testid": "quantity",
        "css": "form#f input[name=quantity]",
        "nearby": "Quantity"
      }
    },
    {
      "t": 1762070414700,
      "type": "paste",
      "url": "http://localhost:8002/incoming/new",
      "value": "PO-1022",
      "el": {
        "tag": "input",
        "role": "textbox",
        "name": "reference",
        "label": "Reference",
        "testid": "reference",
        "css": "form#f input[name=reference]",
        "nearby": "Reference"
      }
    },
    {
      "t": 1762070415900,
      "type": "submit",
      "url": "http://localhost:8002/incoming/new",
      "el": {
        "tag": "form",
        "role": "form",
        "name": null,
        "label": null,
        "testid": null,
        "css": "form#f",
        "nearby": "Save"
      }
    },
    {
      "t": 1762070415950,
      "type": "net",
      "method": "POST",
      "url": "/api/incoming",
      "status": 201,
      "req": {
        "product_id": "GAD-217",
        "quantity": 20,
        "reference": "PO-1022"
      },
      "resp": {
        "id": 1,
        "schema_version": 1,
        "product_id": "GAD-217",
        "quantity": 20,
        "reference": "PO-1022"
      }
    }
  ]
}
```

## Supplier Inbox API (from its /openapi.json)

```json
{
 "paths": {
  "/api/messages": {
   "get": {
    "summary": "List Messages",
    "operationId": "list_messages_api_messages_get",
    "parameters": [
     {
      "name": "since",
      "in": "query",
      "required": false,
      "schema": {
       "type": "string",
       "default": "",
       "title": "Since"
      }
     },
     {
      "name": "limit",
      "in": "query",
      "required": false,
      "schema": {
       "anyOf": [
        {
         "type": "integer"
        },
        {
         "type": "null"
        }
       ],
       "title": "Limit"
      }
     }
    ],
    "responses": {
     "200": {
      "description": "Successful Response",
      "content": {
       "application/json": {
        "schema": {
         "type": "array",
         "items": {
          "$ref": "#/components/schemas/Message"
         },
         "title": "Response List Messages Api Messages Get"
        }
       }
      }
     },
     "422": {
      "description": "Validation Error",
      "content": {
       "application/json": {
        "schema": {
         "$ref": "#/components/schemas/HTTPValidationError"
        }
       }
      }
     }
    }
   }
  }
 },
 "components": {
  "schemas": {
   "Message": {
    "properties": {
     "id": {
      "type": "string",
      "title": "Id"
     },
     "sender": {
      "type": "string",
      "title": "Sender"
     },
     "sender_domain": {
      "type": "string",
      "title": "Sender Domain"
     },
     "subject": {
      "type": "string",
      "title": "Subject"
     },
     "body": {
      "type": "string",
      "title": "Body"
     },
     "received_at": {
      "type": "string",
      "title": "Received At"
     }
    },
    "type": "object",
    "required": [
     "id",
     "sender",
     "sender_domain",
     "subject",
     "body",
     "received_at"
    ],
    "title": "Message"
   }
  }
 }
}
```

## Recent messages (`GET /api/messages`, first six)

```json
[
 {
  "id": "msg-0001",
  "sender": "procurement@globex.com",
  "sender_domain": "globex.com",
  "subject": "Globex order PO-2033",
  "body": "Hello Stockroom,\n\nThis is Globex purchase order PO-2033. Please receive 34 units of item WID-114 at your earliest convenience.\n\nRegards,\nGlobex Procurement",
  "received_at": "2026-11-02T08:35:16Z"
 },
 {
  "id": "msg-0002",
  "sender": "erp-noreply@initech.com",
  "sender_domain": "initech.com",
  "subject": "[ERP] PO-3030",
  "body": "ORDER REF=PO-3030 | PART=GAD-200 | UNITS=77\nSent automatically from Initech ERP",
  "received_at": "2026-11-02T08:47:15Z"
 },
 {
  "id": "msg-0003",
  "sender": "erp-noreply@initech.com",
  "sender_domain": "initech.com",
  "subject": "[ERP] PO-3039",
  "body": "ORDER REF=PO-3039 | PART=GAD-204 | UNITS=98\nSent automatically from Initech ERP",
  "received_at": "2026-11-02T09:13:11Z"
 },
 {
  "id": "msg-0004",
  "sender": "erp-noreply@initech.com",
  "sender_domain": "initech.com",
  "subject": "[ERP] PO-3014",
  "body": "ORDER REF=PO-3014 | PART=WID-121 | UNITS=44\nSent automatically from Initech ERP",
  "received_at": "2026-11-02T09:21:07Z"
 },
 {
  "id": "msg-0005",
  "sender": "procurement@globex.com",
  "sender_domain": "globex.com",
  "subject": "Globex order PO-2035",
  "body": "Hello Stockroom,\n\nThis is Globex purchase order PO-2035. Please receive 115 units of item WID-107 at your earliest convenience.\n\nRegards,\nGlobex Procurement",
  "received_at": "2026-11-02T09:39:30Z"
 },
 {
  "id": "msg-0006",
  "sender": "erp-noreply@initech.com",
  "sender_domain": "initech.com",
  "subject": "[ERP] PO-3037",
  "body": "ORDER REF=PO-3037 | PART=GAD-205 | UNITS=94\nSent automatically from Initech ERP",
  "received_at": "2026-11-02T10:00:40Z"
 }
]
```

## Stockroom API (its /openapi.json)

```json
{
 "openapi": "3.1.0",
 "info": {
  "title": "Stockroom",
  "version": "1.0.0"
 },
 "paths": {
  "/api/incoming": {
   "post": {
    "summary": "Create Incoming",
    "operationId": "create_incoming_api_incoming_post",
    "requestBody": {
     "required": true,
     "content": {
      "application/json": {
       "schema": {
        "$ref": "#/components/schemas/IncomingV1"
       }
      }
     }
    },
    "responses": {
     "201": {
      "description": "Successful Response",
      "content": {
       "application/json": {
        "schema": {
         "type": "object",
         "additionalProperties": true,
         "title": "Response Create Incoming Api Incoming Post"
        }
       }
      }
     },
     "422": {
      "description": "Validation Error",
      "content": {
       "application/json": {
        "schema": {
         "$ref": "#/components/schemas/HTTPValidationError"
        }
       }
      }
     }
    }
   },
   "get": {
    "summary": "List Incoming",
    "operationId": "list_incoming_api_incoming_get",
    "parameters": [
     {
      "name": "reference",
      "in": "query",
      "required": false,
      "schema": {
       "anyOf": [
        {
         "type": "string"
        },
        {
         "type": "null"
        }
       ],
       "title": "Reference"
      }
     }
    ],
    "responses": {
     "200": {
      "description": "Successful Response",
      "content": {
       "application/json": {
        "schema": {
         "type": "array",
         "items": {
          "type": "object",
          "additionalProperties": true
         },
         "title": "Response List Incoming Api Incoming Get"
        }
       }
      }
     },
     "422": {
      "description": "Validation Error",
      "content": {
       "application/json": {
        "schema": {
         "$ref": "#/components/schemas/HTTPValidationError"
        }
       }
      }
     }
    }
   }
  },
  "/api/products": {
   "get": {
    "summary": "List Products",
    "operationId": "list_products_api_products_get",
    "responses": {
     "200": {
      "description": "Successful Response",
      "content": {
       "application/json": {
        "schema": {
         "items": {
          "additionalProperties": true,
          "type": "object"
         },
         "type": "array",
         "title": "Response List Products Api Products Get"
        }
       }
      }
     }
    }
   }
  },
  "/api/products/{sku}": {
   "get": {
    "summary": "Get Product",
    "operationId": "get_product_api_products__sku__get",
    "parameters": [
     {
      "name": "sku",
      "in": "path",
      "required": true,
      "schema": {
       "type": "string",
       "title": "Sku"
      }
     }
    ],
    "responses": {
     "200": {
      "description": "Successful Response",
      "content": {
       "application/json": {
        "schema": {
         "type": "object",
         "additionalProperties": true,
         "title": "Response Get Product Api Products  Sku  Get"
        }
       }
      }
     },
     "422": {
      "description": "Validation Error",
      "content": {
       "application/json": {
        "schema": {
         "$ref": "#/components/schemas/HTTPValidationError"
        }
       }
      }
     }
    }
   }
  }
 },
 "components": {
  "schemas": {
   "HTTPValidationError": {
    "properties": {
     "detail": {
      "items": {
       "$ref": "#/components/schemas/ValidationError"
      },
      "type": "array",
      "title": "Detail"
     }
    },
    "type": "object",
    "title": "HTTPValidationError"
   },
   "IncomingV1": {
    "properties": {
     "product_id": {
      "type": "string",
      "title": "Product Id"
     },
     "quantity": {
      "type": "integer",
      "exclusiveMinimum": 0.0,
      "title": "Quantity"
     },
     "reference": {
      "type": "string",
      "title": "Reference"
     }
    },
    "additionalProperties": false,
    "type": "object",
    "required": [
     "product_id",
     "quantity",
     "reference"
    ],
    "title": "IncomingV1"
   },
   "ValidationError": {
    "properties": {
     "loc": {
      "items": {
       "anyOf": [
        {
         "type": "string"
        },
        {
         "type": "integer"
        }
       ]
      },
      "type": "array",
      "title": "Location"
     },
     "msg": {
      "type": "string",
      "title": "Message"
     },
     "type": {
      "type": "string",
      "title": "Error Type"
     },
     "input": {
      "title": "Input"
     },
     "ctx": {
      "type": "object",
      "title": "Context"
     }
    },
    "type": "object",
    "required": [
     "loc",
     "msg",
     "type"
    ],
    "title": "ValidationError"
   }
  }
 }
}
```
