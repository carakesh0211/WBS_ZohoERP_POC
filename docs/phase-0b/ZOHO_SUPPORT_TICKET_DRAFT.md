# Zoho Catalyst support ticket — DRAFT, NOT SENT

**Status: DRAFT.** This ticket has **not** been raised. It is prepared for a named person to review and submit.

**Submit to:** `support@zohocatalyst.com` (or the Catalyst console's support channel)
**Suggested subject:** *Outbound network egress policy and static IP/CIDR availability for AppSail and Functions — India DC*

## Before you send

1. **Check the two placeholders** — `[YOUR NAME]` and `[YOUR ROLE]` at the end.
2. **Decide whether to name the client.** The draft says "a customer in the manufacturing sector" rather than naming Atha Group. Change it if your engagement terms allow, or leave it.
3. **Question 3 is the one that matters most.** If the reply answers everything except whether a published range is contractual and whether it may change without notice, the ticket has not achieved its purpose — press for that specifically.
4. **Record the ticket reference** in the Phase 0B evidence manifest. Per the plan's sanitisation rule, commit the *reference*, not the ticket body, unless the body is purely technical.

---

## Ticket body — copy from here

Hello,

We are evaluating Zoho Catalyst as the application platform for a financial control system and need to confirm the outbound network egress policy before committing to an architecture. Our questions are specific and technical; we would value precise answers rather than general guidance, because an architectural decision depends on them.

### Our environment

| | |
|---|---|
| Data centre | **India (IN)** — `console.catalyst.zoho.in` |
| Project name | `WBS-ZohoERP-POC` |
| Project ID | `4239000000062001` |
| AppSail service | `wbs-platform-spike` |
| AppSail service ID | `4239000000096001` |
| Runtime | Python 3.13, Catalyst-Managed Runtime |
| Environment | Development |

### What we are trying to do

The application needs to connect from Catalyst to an **external managed PostgreSQL database** over TLS on port 5432 (and 6543 for a transaction pooler). Both execution surfaces need this: **AppSail** for request handling, and **Cron or Event Functions** for scheduled background work.

We have reviewed the published documentation, including the Database Connector CodeLib page, which describes connecting to an external MySQL or PostgreSQL database from **Catalyst Serverless Functions**. We could not find equivalent guidance for **AppSail**, nor any page describing the outbound egress policy for either surface. We are therefore asking directly rather than inferring from absence.

### Questions

**1 — Outbound TCP**
Is arbitrary outbound TCP from an AppSail container permitted, specifically on ports **5432** and **6543**? Is there any port restriction, protocol restriction, egress proxy, or outbound allowlist we should be aware of?

**2 — Static egress IP or CIDR**
Is there a **static egress IP address or CIDR range** — per project, per data centre, or per account — that we can allowlist on a third-party database service, so that the database is not reachable from the public internet?

**3 — Is that commitment contractual, and can it change?**
If such a range exists, we need to know two further things:

- **Is it contractual, or best-effort?** Is it stated in an SLA, a support commitment, or documentation we can cite?
- **May the published range change without notice?** If it can change, what notice period applies, and how would we be informed?

*We are asking because an IP range that may change silently cannot be used as an allowlist for a financial system. Unplanned egress-IP rotation would sever the database connection with no warning and no diagnostic signal. If the answer is "best-effort, subject to change", please say so plainly — that is a usable answer and we will design around it.*

**4 — Private network path**
If no static egress range is available, is a **private network path** available or planned — VPC peering, private link, a private endpoint, or an equivalent — for reaching an external database without traversing the public internet?

**5 — Do Functions differ from AppSail?**
Are the answers to questions 1 to 4 **identical for Cron and Event Functions**, or do those surfaces have a different egress policy, different source addresses, or different restrictions? We cannot assume parity, since the Database Connector documentation covers Functions but not AppSail.

**6 — TLS verification**
Is outbound TLS with **full certificate chain validation and hostname verification** supported from both surfaces? May we ship a custom CA bundle inside the deployment bundle for the database provider's certificate authority?

**7 — Outbound connection limits**
Are there documented limits on outbound connections — concurrent sockets per instance, total connections per project, or a maximum connection lifetime?

**8 — Long-lived connection handling**
Does the platform terminate long-lived outbound connections, for example idle database connections? If so, after what period? This affects whether we can use connection pooling, or must open a new connection per invocation.

### Why we are asking rather than testing

We can and will test reachability empirically. But testing cannot answer questions 3, 4, 7 or 8: repeated observation of the same source address does not establish that the address is static, and no amount of testing reveals a change policy. Those need an authoritative answer from Zoho, which is why we are asking rather than assuming.

We would rather design correctly around a documented limitation than discover it in production.

Thank you — we are happy to provide any further detail about our configuration.

Best regards,
**[YOUR NAME]**
**[YOUR ROLE]**
On behalf of a customer in the manufacturing sector evaluating Zoho Catalyst and Zoho ERP

## Ticket body ends

---

## How the answers map to the gate

| Question | Determines |
|---|---|
| 1 | **Q-A.** Whether the architecture is viable at all |
| 2, 3, 4 | **Q-B.** Whether the path can be secured by network controls. **Q-B cannot pass without an authoritative answer here** |
| 5 | Whether Stage 2 (Functions) needs a separate design. Phase 0B cannot clear without it |
| 6 | Whether TLS verification is achievable, or the security posture must be reconsidered |
| 7, 8 | Phase 1 connection-pooling design under scale-to-zero |

**If question 3 comes back as "best-effort, subject to change without notice", Q-B fails on route 1.** The fallback is then route 2 (a private path, question 4) or route 3 (a client-approved architecture accepting a publicly reachable database secured by TLS and credentials alone). That is a client security decision, and it should be put to them with the Zoho answer attached.

## Recording the response

Per the plan's evidence-sanitisation rule:

- commit the **ticket reference number**, not the ticket body
- quote Zoho's answers **verbatim only where purely technical**; summarise commercial or confidential passages and note that the full text is held outside the repository
- **never commit** support-agent names or email addresses, account identifiers, or any screenshot that would let a reader reach the resource
