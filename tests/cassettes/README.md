# Adapter cassettes

## Provenance: every cassette here is INVENTED, not recorded

**No Zoho tenant has been contacted.** Phase 0B has not cleared, and the
standing boundary for this build is absolute: no live Zoho tenant, no Catalyst,
no Supabase, no cloud or billable resource of any kind. So there is no recorded
traffic to replay.

Every file under this directory was therefore **written by hand**, modelled on
the response shapes described in Zoho's published documentation, and every one
of them carries `"provenance": "INVENTED-SANITISED"` in its envelope. Nothing
here is evidence about how a real tenant behaves. What these cassettes are
evidence *for* is our own mapping, pagination, capability routing and product
isolation — the parts that are ours to get right.

`tests/test_integration_product_isolation.py` asserts that every cassette
declares this provenance, so a real recording cannot be dropped in silently
later: it would have to change the label, and changing the label is a visible
diff that says a tenant was contacted.

**Organisation ids, vendor names, document numbers and amounts are fictional.**
No client data appears in this directory.

## Layout

Cassettes are filed by **product**, and the directory name is load-bearing:

```
tests/cassettes/erp/                 -> product "ERP"
tests/cassettes/books_inventory/     -> product "BOOKS_INVENTORY"
```

`CassetteTransport` refuses to load a cassette whose declared `product` does
not match the directory it sits in. That is the first of three independent
locks against products being mixed; the other two — the recorded `base_url` and
the recorded OAuth `scope` — are checked on every request, and they survive
someone editing the `product` label. See
`app.backend.integration.adapter.ProductMixingError`.

## Envelope

```jsonc
{
  "cassette_version": 1,
  "product":       "ERP" | "BOOKS_INVENTORY",
  "service":       "erp" | "books" | "inventory",   // NOT the same as product
  "api_version":   "v3" | "v1",
  "provenance":    "INVENTED-SANITISED",
  "provenance_note": "why this shape, and what it is and is not evidence of",
  "request": {
    "method":   "GET" | "POST",
    "base_url": "https://www.zohoapis.in/erp/v3",   // lock 2
    "path":     "/bills",
    "scope":    "ERP.bills.READ",                   // lock 3
    "page":     1
  },
  "response": { "status": 200, "body": { ... } }
}
```

The index key is `(method, path, page)`. `page` defaults to 1, so a detail
endpoint needs no page.

## Monetary values are JSON numbers on purpose

Zoho sends `"total": 147500.59`, a JSON number, and that is what these files
contain. `CassetteTransport` parses them with `json.loads(..., parse_float=str)`
so the value reaches `dto.paise()` as the exact string `"147500.59"` and is
converted once, half-up, by `app.backend.money.to_paise`. A monetary value
never exists as a float, not even for the moment between parsing and mapping —
which is the rule AUD-H-007 established and `dto.paise()` re-asserts by
rejecting a float outright.
