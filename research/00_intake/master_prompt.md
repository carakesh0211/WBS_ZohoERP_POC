# Master Prompt: SAP WBS Research, SAP-Familiar UI and Zoho ERP Connector-Ready CAPEX/CWIP POC Blueprint

## Role

Act as a senior solution architect with deep experience in:

- SAP Project System, especially Project Definition, WBS, Networks, Activities, Milestones, Budgeting, Availability Control, Commitments, Actuals, Settlement, Asset Under Construction and Fixed Asset Capitalisation
- Core manufacturing and greenfield plant setup projects
- CAPEX governance, CWIP accounting, project procurement and management reporting
- Zoho ERP, Zoho Finance applications, Zoho Analytics, REST APIs, webhooks, custom functions and external application integration
- Enterprise application architecture, internal controls, audit trail, cybersecurity and implementation planning

## Objective

Study SAP WBS and SAP Project System in sufficient depth to design an independent, API-first CAPEX and CWIP Project Control application that can integrate with Zoho ERP.

This phase is for research, functional analysis and POC blueprint preparation only.

Do not build the application in this phase.

The final output must be detailed enough to be handed to Codex, Claude Code or another software engineering agent to build a working Proof of Concept without having to reinterpret the business requirements.

## Primary Requirement Document

Read the attached file carefully:

`Atha_Group_CWIP_CAPEX_Budget_Control_Branded.pdf`

Treat this document as the primary source for the client's stated requirements.

Preserve the terminology, calculations, controls, workflow and expected outcomes contained in the document. Do not silently replace or dilute any requirement.

Where the document is incomplete, ambiguous or internally inconsistent:

1. Identify the issue explicitly.
2. Record it under Assumptions or Open Questions.
3. Propose a recommended treatment.
4. Do not present the recommendation as a confirmed client requirement.

## Client Context

The client is a core manufacturing organisation setting up a new plant.

The requirement is not merely project task management. The intended solution must support financial and operational control over the complete CAPEX lifecycle, including:

- CAPEX project creation
- WBS hierarchy
- Budgeting by project, WBS element and budget head
- Purchase Request control
- Purchase Order commitment
- GRN or Purchase Receipt monitoring
- Vendor Bill and CWIP accounting
- Budget revisions
- Overrun and exception approval
- Project completion
- CWIP settlement and fixed asset capitalisation
- Management dashboards
- Complete audit trail

The preferred direction is an independent application integrated with Zoho ERP rather than forcing the entire requirement through multiple disconnected Zoho applications.

## Mandatory Research Instructions

Research SAP Project System and WBS using current, authoritative sources.

Prioritise:

1. Official SAP Help Portal documentation
2. SAP Learning documentation
3. Official SAP product and implementation documentation
4. Official Zoho ERP and Zoho API documentation
5. Recognised accounting and project-control references where required

Use third-party blogs only as supplementary material.

Every major SAP or Zoho product capability mentioned in the report must be supported by a source reference.

Clearly distinguish:

- Confirmed SAP standard functionality
- Confirmed Zoho ERP standard functionality
- Configuration possibilities
- Custom application functionality
- Assumptions
- Recommended design choices
- Items requiring vendor confirmation

Do not rely only on general knowledge.

## SAP WBS Capability Areas to Study

Study and explain at least the following areas in SAP Project System:

### 1. Project Structure

- Project Definition
- WBS hierarchy
- Coding masks and project numbering
- WBS levels
- Operative and statistical WBS elements
- Networks and network activities
- Milestones
- Relationships and dependencies
- Project templates and standard structures
- Project versions and baselines

### 2. Organisational and Accounting Assignment

- Company code
- Controlling area
- Plant
- Business area or segment
- Profit centre
- Cost centre
- Functional area
- Responsible person
- Project profile
- Investment profile
- Asset class
- Settlement profile

### 3. Planning and Budgeting

- Cost planning
- Revenue planning, where relevant
- Overall budget
- Annual budget
- Original budget
- Supplements
- Returns
- Transfers
- Released budget
- Budget versions
- Planning versus budgeting
- Budget availability control
- Tolerance limits
- Warning versus hard-stop controls
- Assigned value
- Available budget

### 4. Commitments and Actual Costs

- Purchase Requisition commitments
- Purchase Order commitments
- Goods Receipt treatment
- Service Entry Sheet treatment
- Vendor Invoice treatment
- Partial receipt and partial billing
- PO amendment
- PO cancellation
- PO closure
- Unplanned delivery cost
- Freight, duties, taxes and landed cost
- Down payments and advances
- Retention money
- Contract variations and change orders
- Internal labour and activity allocation
- Material issue to project
- Stock transfer to project
- Material return
- Internal orders, if relevant
- Avoidance of double counting between commitments and actuals

### 5. Status and Governance

- User status
- System status
- Release
- Technical completion
- Closed status
- Locking and reopening
- Approval governance
- Change control
- Budget revision
- Exception approval
- Delegation of authority
- Audit log

### 6. CWIP, Asset Under Construction and Capitalisation

- CWIP accumulation
- Asset Under Construction
- Investment measures
- Settlement rules
- Periodic settlement
- Full settlement
- Settlement to multiple fixed assets
- Capitalisation date
- Asset componentisation
- Common cost allocation
- Pre-operative expense allocation
- Project closure
- Treatment of abandoned or cancelled projects

### 7. Reporting and Project Control

- Budget versus commitment versus actual
- Available budget
- Cost variance
- Schedule variance
- Project progress
- Earned Value Management, where applicable
- Open commitment ageing
- Pending GRN
- Pending invoice
- Project cash flow
- Project forecast
- CWIP ageing
- Capitalisation pending
- Drill-down to source transactions

## Requirement Extraction from the Attached Document

Create a structured requirement catalogue from the attached PDF.

At minimum, capture:

- Requirement ID
- Requirement description
- Source section or page
- Business purpose
- Actor
- Trigger
- Preconditions
- Input data
- Business rule
- Workflow
- Approval requirement
- Accounting impact
- Integration dependency
- Report or alert requirement
- Priority
- Ambiguity or open question
- POC inclusion status

The requirement catalogue must include the following controls stated in the document:

### Core Formula

`Available Budget = Current Approved Budget - Actual CWIP - Open PO Commitments`

Also evaluate whether Purchase Requests should reserve budget before PO creation. The attached document requires budget validation at PR and PO stages but defines the stated commitment formula primarily using open Purchase Orders. Treat this as an explicit design decision and open question.

### Mandatory Lifecycle

1. CAPEX or CWIP project creation
2. Budget approval
3. Purchase Request
4. Budget availability check
5. Normal or exception approval
6. Purchase Order as commitment
7. Purchase Receipt or GRN
8. Vendor Bill as actual CWIP
9. Project completion review
10. Fixed asset capitalisation

### Commitment-to-Actual Logic

The design must prevent double counting.

It must cover at least:

- PO approved, not billed
- Partially billed PO
- Fully billed PO
- Partially received but not billed
- Fully received but not billed
- PO amended upward
- PO amended downward
- PO cancelled before billing
- PO closed with residual value
- Vendor bill exceeding PO
- Credit note
- Debit note
- Bill reversal
- GRN reversal
- Foreign currency PO and exchange-rate variance
- Tax and non-creditable tax treatment
- Freight and other landed costs

### Budget Revision Logic

The original approved budget must remain immutable.

The application must separately record:

- Original budget
- Approved supplements
- Approved reductions or returns
- Approved transfers
- Current approved budget
- Pending revision
- Rejected revision
- Revision reason
- Requestor
- Approver
- Approval date
- Approval reference
- Effective date
- Version number

### Key Controls

Include:

- Mandatory CAPEX code and budget head
- Revalidation on PO amendment
- Release of unused commitment on cancellation or closure
- Conversion of commitment to actual without duplication
- Prevention of procurement against closed or capitalised projects
- Controlled reopening
- Drill-down from project to PR, PO, GRN, bill and asset
- Threshold alerts
- Complete maker-checker audit trail

## Target Solution Direction

Design an independent web application tentatively referred to as:

`CAPEX & WBS Control Hub`

The name may be changed in the report if a better product name is recommended.

The solution must be:

- API-first
- Modular
- Multi-entity
- Multi-plant
- Multi-project
- Multi-level WBS capable
- Role-based
- Auditable
- Reconciliation-driven
- Suitable for integration with Zoho ERP
- Capable of later integration with other ERPs
- Designed so that Zoho ERP remains the accounting and procurement system of record unless the report recommends otherwise with justification

Do not assume that Zoho Creator is the default build platform.

Evaluate at least these options:

1. Standalone custom web application integrated with Zoho ERP
2. Zoho Creator-based control layer
3. Hybrid architecture
4. Configuration-only approach inside Zoho ERP

Compare the options and recommend one for the POC and one for long-term production use.


## SAP-Familiar User Experience and Visual Design Requirement

A critical objective of the future application is user adoption.

The intended users may already be familiar with SAP Project System, SAP WBS and SAP-style enterprise transaction processing. The new application must therefore provide a familiar enterprise experience so that users do not feel that they have moved into an unrelated or lightweight application.

The future POC and production application should use a **SAP-familiar interaction model and visual language**, while remaining an independent application.

This does not mean copying SAP source code, proprietary assets, logos, trademarks, copyrighted illustrations or exact screen designs. The solution must not falsely represent itself as an SAP product.

The design objective is:

> Create an original enterprise application whose information density, navigation conventions, visual hierarchy, transaction behaviour and project-control terminology feel familiar to experienced SAP WBS users.

### Mandatory UI/UX Research

Study the current official SAP user-experience design system and SAP Project System user-interface patterns using authoritative SAP sources.

Research at least:

- SAP Fiori design principles
- Current SAP visual themes and design guidance
- SAP Launchpad or enterprise shell patterns
- List Report patterns
- Object Page patterns
- Analytical List Page patterns
- Worklist patterns
- Overview Page patterns
- Wizard and guided-process patterns
- Enterprise data tables
- Smart filters and filter bars
- Value-help or lookup dialogs
- Variant management
- Personalised views
- Saved filters
- Mass actions
- Status indicators
- Message handling
- Notifications
- Approval inbox patterns
- Breadcrumbs
- Tabs and section navigation
- Side panels
- Drill-down patterns
- Master-detail layouts
- Compact and cosy display density
- Desktop-first enterprise application behaviour
- Keyboard navigation
- Accessibility
- Responsive behaviour
- SAP WBS, budgeting and project-control screen conventions

Use official SAP design documentation wherever available.

Clearly distinguish between:

- General SAP Fiori design patterns
- SAP Project System or WBS-specific interaction patterns
- Design recommendations
- Features confirmed as standard SAP behaviour
- Features that are only proposed for the custom application

### Visual Design Direction

The proposed application should have an enterprise-grade visual language comparable to the experience of using SAP WBS.

The UI blueprint must define:

- Primary and secondary colour palette
- Neutral background colours
- Table header treatment
- Input field treatment
- Button hierarchy
- Status colours
- Warning, error and success states
- Typography
- Font scale
- Spacing system
- Border treatment
- Shadow usage
- Iconography
- Card treatment
- Toolbar treatment
- Page header treatment
- Section header treatment
- Modal and drawer treatment
- Charts and dashboard presentation
- Density modes
- Dark mode, if recommended
- Print and PDF-friendly views

The colour scheme may be inspired by recognised enterprise ERP interfaces, but it must be original and must not reproduce SAP proprietary branding.

Do not use SAP logos or imply SAP endorsement.

### Design Principles

The future application must follow these principles:

1. **SAP familiarity without imitation**
   - Use familiar enterprise patterns.
   - Do not create a pixel-for-pixel clone.
   - Do not copy proprietary SAP assets or screen layouts.

2. **High information density**
   - Support accountants, project controllers, procurement users and plant users who need to see many fields at once.
   - Avoid excessive whitespace and oversized consumer-app controls.
   - Provide a compact desktop mode.

3. **Transaction clarity**
   - Every screen must clearly show document number, status, project, WBS, entity, plant, amount, currency, owner, approval status and audit information where relevant.

4. **Progressive disclosure**
   - Show critical information first.
   - Allow drill-down into detailed accounting, procurement and audit information.

5. **Consistency**
   - The same WBS, project, budget, approval and status terminology must be used throughout the application.

6. **Control visibility**
   - Budget availability, commitment, actual, variance and exception status must be visible before the user commits a transaction.

7. **Auditability**
   - Users must be able to navigate from a summary value to the underlying PR, PO, GRN, bill, budget revision, approval and asset record.

8. **Low training effort**
   - An SAP-experienced user should be able to understand the main navigation and transaction flow with minimal training.

9. **Accounting precision**
   - Numbers, signs, decimal places, currency, Indian numbering format, dates and totals must be unambiguous.

10. **Role-based experience**
   - A requestor, project manager, procurement user, finance user, CFO and auditor should each see an appropriately focused workspace.

### Mandatory Application Shell

Design an enterprise application shell containing:

- Top shell bar
- Application or company identity
- Entity and plant selector
- Global search
- Notification centre
- Approval inbox
- User profile
- Help
- Left-side navigation or equivalent enterprise navigation
- Breadcrumb trail
- Context-sensitive page title
- Object status
- Page-level actions
- Recent objects
- Favourite objects
- Saved views
- Role-based landing page

The shell should permit future integration with Zoho ERP while preserving the identity of the independent application.

### Proposed Main Navigation

The research report should validate and refine a navigation structure such as:

- Home
- My Work
- Approvals
- CAPEX Projects
- WBS Structures
- Budgets
- Budget Revisions
- Purchase Requests
- Commitments
- GRN and Receipts
- Vendor Bills and Actuals
- CWIP
- Capitalisation
- Reconciliation
- Reports
- Alerts and Exceptions
- Master Data
- Integration Monitor
- Zoho ERP Setup
- Connection Profiles
- Organisation Mapping
- Field and Dimension Mapping
- Sync Configuration
- API Health and Usage
- Audit Trail
- Administration

### Mandatory Screen Catalogue

Prepare a detailed UI specification for at least the following screens:

1. Executive CAPEX Dashboard
2. Project Controller Workbench
3. My Approval Inbox
4. CAPEX Project List
5. CAPEX Project Object Page
6. WBS Hierarchy Explorer
7. WBS Tree Table
8. WBS Element Detail Page
9. Budget Planning Grid
10. Budget Version Comparison
11. Budget Revision Request
12. Budget Transfer Screen
13. Budget Availability Check
14. Purchase Request Control View
15. Purchase Order Commitment View
16. GRN and Unbilled Receipt View
17. Vendor Bill and Actual CWIP View
18. Commitment-to-Actual Reconciliation
19. CWIP Ledger
20. Project Completion Review
21. Capitalisation Workbench
22. Asset Allocation Screen
23. Open Commitment Ageing
24. CWIP Ageing
25. Exception and Overrun Monitor
26. Integration Event Monitor
27. Reconciliation Exception Queue
28. Audit Trail Viewer
29. Approval Matrix Configuration
30. Master Data Configuration
31. Zoho ERP Connection Setup Wizard
32. Zoho OAuth Authorisation and Consent Screen
33. Zoho Organisation Selection and Mapping
34. API Scope and Permission Validation
35. Master Data Mapping Workbench
36. Transaction Field Mapping Workbench
37. Sync Direction and Scheduling Configuration
38. Integration Health and API Usage Dashboard
39. Failed Sync and Retry Queue
40. Connector Audit and Credential Activity Log

For every screen, specify:

- Screen purpose
- Primary users
- Entry points
- Header fields
- Search and filter fields
- Table columns
- Form sections
- Tabs
- Actions
- Mass actions
- Status indicators
- Validations
- Warning messages
- Error messages
- Drill-down links
- Attachments
- Comments
- Audit information
- Permission requirements
- Empty state
- Loading state
- Failure state
- Mobile or tablet behaviour
- Export and print behaviour

### WBS Hierarchy Experience

The WBS interface must be a major differentiator.

Design an SAP-familiar WBS workbench with:

- Expandable and collapsible hierarchy
- Tree-table presentation
- Configurable WBS levels
- Project and WBS coding
- Parent-child relationships
- Drag-and-drop only where control-safe
- Add child WBS
- Copy WBS
- Move WBS through controlled workflow
- Freeze WBS
- Close WBS
- Reopen WBS
- Budget allocation by WBS
- Planned and actual dates
- Responsible person
- Status
- Progress
- Budget
- Commitment
- Actual
- Available budget
- Forecast
- Variance
- Asset category
- Settlement receiver
- Procurement allowed indicator
- Posting allowed indicator

The user should be able to switch between:

- Hierarchy view
- Financial view
- Schedule view
- Procurement view
- Capitalisation view
- Audit view

### Enterprise Data Table Requirements

Tables must support:

- Column sorting
- Multi-column filtering
- Advanced filter builder
- Column resizing
- Column reordering
- Column pinning
- Grouping
- Subtotals
- Grand totals
- Hierarchical rows
- Expand and collapse
- Saved variants
- Personal and shared variants
- Export to Excel
- Export to CSV
- Print
- Full-screen mode
- Dense display
- Pagination or virtual scrolling
- Row-level actions
- Mass actions
- Conditional formatting
- Status icons
- Links to source transactions

### Budget Planning Grid

The budget screen should resemble an enterprise planning workbench.

It should support:

- Project and WBS hierarchy on the left
- Budget heads across rows or columns
- Original budget
- Supplement
- Return
- Transfer in
- Transfer out
- Current approved budget
- PR reservation
- PO commitment
- Actual CWIP
- Available budget
- Forecast at completion
- Variance
- Exposure percentage
- Comments
- Version comparison
- Period-wise values
- Annual and overall budget
- Freeze and release controls
- Spreadsheet-like keyboard entry where appropriate
- Import and validation
- Controlled bulk upload
- Error cells and validation messages

### Transaction Object Page Pattern

Every major object should have a consistent object page.

Suggested page header:

- Object type
- Object number
- Description
- Status
- Entity
- Plant
- Project
- WBS
- Owner
- Amount
- Currency
- Approval status
- Zoho ERP reference
- Created by
- Last updated

Suggested tabs or sections:

- Overview
- Financials
- Procurement
- Accounting
- Approvals
- Attachments
- Comments
- Integration
- Reconciliation
- Audit Trail

### Dashboard Requirements

Dashboards must provide:

- Summary tiles
- Analytical charts
- Tabular drill-down
- Filters
- Saved views
- Role-based content
- Alert indicators
- Direct navigation to exceptions

Use enterprise charts suitable for financial and project control, including:

- Budget versus commitment versus actual
- Available budget
- Exposure percentage
- Project progress
- CWIP ageing
- Commitment ageing
- Budget revision trend
- Plant-wise CAPEX
- Department-wise CAPEX
- Budget-head-wise variance
- Projects awaiting capitalisation
- Integration failures

Avoid decorative charts that do not support management action.

### Status and Message Design

Define a status model with consistent text and visual treatment.

At minimum, include:

- Draft
- Submitted
- Under Review
- Approved
- Rejected
- Returned
- Released
- Partially Committed
- Fully Committed
- Partially Actualised
- Fully Actualised
- Budget Exceeded
- Exception Pending
- Technically Completed
- Financially Completed
- Awaiting Capitalisation
- Capitalised
- Closed
- Reopened
- Integration Failed
- Reconciliation Pending

Messages must be actionable.

Example:

Instead of:

`Budget error`

Use:

`The proposed PO value of ₹20,00,000 exceeds the available budget of ₹15,00,000 for WBS CAPEX-2026-001.03 by ₹5,00,000. Submit a budget revision or request exception approval.`

### SAP-Familiar Terminology Mapping

Prepare a terminology matrix showing:

- SAP term
- Proposed application term
- Zoho ERP term
- Client-preferred term
- Tooltip or help text

At minimum, include:

- Project Definition
- WBS Element
- Network
- Activity
- Milestone
- Budget
- Assigned Value
- Commitment
- Actual Cost
- Available Budget
- Availability Control
- Settlement Rule
- Asset Under Construction
- Technical Completion
- Closed
- User Status
- System Status

Avoid unnecessarily renaming familiar concepts.

### UI Prototype Deliverables

The research and blueprint report must include:

- Design direction
- Colour and typography tokens
- Layout grid
- Component inventory
- Navigation map
- Screen inventory
- Low-fidelity wireframes
- High-fidelity mock-up instructions
- Responsive behaviour
- Accessibility requirements
- UI acceptance criteria
- Recommended frontend component library
- Mapping between component-library controls and SAP-familiar patterns

Use Mermaid for navigation flows and page relationships where practical.

For the later POC build, the coding agent should create working screens rather than static images only.

### UI Technology Evaluation

Evaluate frontend frameworks and component libraries for creating an SAP-familiar enterprise interface.

Consider at least:

- SAP OpenUI5, where licensing and technical fit permit
- SAP Fundamental Styles or officially available SAP-oriented UI resources, subject to current licensing verification
- React with an enterprise component library
- Angular with an enterprise component library
- Vue with an enterprise component library
- Custom design system using documented design tokens

For each option, assess:

- Licensing
- SAP visual familiarity
- Development speed
- Component maturity
- Tree-table capability
- Data-grid capability
- Accessibility
- Theming
- Responsiveness
- Performance
- Maintainability
- Vendor lock-in
- Developer availability
- Suitability for Zoho ERP integration

Do not recommend a library until its current licence and production suitability have been verified.

### Branding Requirement

The final application should have its own product identity.

Create recommendations for:

- Product name
- Logo direction
- Application icon
- Favicon
- Login screen
- Neutral enterprise branding
- Optional client branding
- White-label capability

The branding should complement the SAP-familiar interface without suggesting that the application is produced, certified or endorsed by SAP.

### Accessibility and Usability

The design should target applicable WCAG standards and include:

- Keyboard navigation
- Visible focus
- Screen-reader labels
- Contrast compliance
- Non-colour status indicators
- Error summary
- Field-level errors
- Logical tab order
- Zoom support
- Accessible data tables
- Accessible charts
- Confirmation for destructive actions
- Undo where control-safe
- Session timeout warning

### UI Performance Requirements

Define measurable targets for:

- Initial page load
- Table loading
- WBS tree expansion
- Filter response
- Dashboard refresh
- Search
- Save
- Approval action
- Export
- Large hierarchy handling

The UI should remain usable with:

- Large projects
- Thousands of WBS elements
- Large PO and bill volumes
- Multiple legal entities and plants
- Long audit histories

### UI Security and Control Requirements

Include:

- Role-based menu visibility
- Field-level permissions
- Entity-level access
- Plant-level access
- Read-only audit views
- Masking of sensitive information
- Prevention of unauthorised mass export
- Session timeout
- Confirmation for high-impact actions
- Reason capture for overrides
- Electronic approval evidence
- Protection against URL manipulation
- Secure file preview and download

### UI Acceptance Criteria

The report must define acceptance criteria such as:

1. An SAP-experienced user can locate a project, open its WBS hierarchy and view budget, commitment and actual values without training assistance.
2. The WBS hierarchy can display at least three levels in a tree table.
3. A user can drill from available budget to PR, PO, GRN, bill and budget revision records.
4. The budget check message identifies the project, WBS, budget head, available amount, proposed amount and shortfall.
5. Tables support saved views, filtering, sorting, export and compact density.
6. Statuses are consistent across all modules.
7. Role-based landing pages display only relevant work and exceptions.
8. The interface is visually enterprise-grade and recognisably SAP-familiar, but contains no SAP logo or misleading SAP branding.
9. The application remains responsive for the POC data volume.
10. Keyboard navigation and accessible error handling are demonstrated.
11. The POC includes at least one complete desktop workflow from project creation to capitalisation.
12. The visual design is implemented consistently across all POC screens.

### UI Handover Package for the Coding Agent

The final Markdown blueprint must include a dedicated UI handover section containing:

- Design principles
- Design tokens
- Colour palette
- Typography
- Spacing scale
- Component list
- Navigation map
- Screen specifications
- Table specifications
- Form specifications
- Status system
- Message catalogue
- Role-based dashboards
- Wireframe guidance
- Responsive rules
- Accessibility checklist
- Frontend architecture recommendation
- Component-library recommendation
- Licensing notes
- UI test cases
- Visual regression testing approach
- Screenshot-based acceptance checklist

The coding agent must be instructed to implement the approved UI system consistently and not improvise unrelated consumer-style screens.



## Dedicated Zoho ERP Integration Setup Module

The future application must include a dedicated, administrator-only setup module through which an authorised implementation administrator can connect the application to one or more Zoho ERP organisations without changing application code.

The connector must support multiple legal entities and multiple Zoho ERP organisation IDs.

The setup experience must be configuration-driven and suitable for both the POC and a future production deployment.

### Setup Module Objectives

The setup module must allow an authorised administrator to:

- Create and manage one or more Zoho ERP connection profiles
- Select the applicable Zoho data centre
- Initiate and complete OAuth 2.0 authorisation
- Securely associate Client ID, Client Secret and Refresh Token
- Discover accessible Zoho ERP organisations
- Select and map the relevant `organization_id`
- Validate the granted OAuth scopes
- Test API connectivity
- Test access to each required Zoho ERP module
- Configure inbound and outbound data flow
- Map Zoho ERP fields to application fields
- Map custom fields, reporting tags, projects and locations
- Configure sync schedules
- Configure polling intervals where events or webhooks are unavailable
- Configure retry and failure rules
- View API usage and rate-limit consumption
- Revoke or rotate credentials
- Temporarily disable a connector
- Run initial master-data synchronisation
- Run reconciliation
- View a complete connector audit trail

### Connection Profile

Design a connection-profile master with at least the following fields:

- Connection Profile ID
- Connection Name
- Legal Entity
- Plant or Business Unit
- Environment
- Zoho Data Centre
- Accounts Domain
- API Domain
- API Version
- Client ID reference
- Client Secret reference
- Redirect URI
- OAuth grant status
- Refresh Token secret reference
- Access Token expiry
- Token last refreshed at
- Zoho Organisation ID
- Zoho Organisation Name
- Base Currency
- Time Zone
- Fiscal Year Start
- Granted Scopes
- Required Scopes
- Missing Scopes
- Connection Status
- Last Successful API Call
- Last Failed API Call
- Last Full Sync
- Last Incremental Sync
- Connector Version
- Created By
- Created At
- Updated By
- Updated At
- Credential Rotated At
- Disabled At
- Disable Reason

Do not store Client Secret, Refresh Token or Access Token as readable plain text in the application database.

Use a secure secret-management design such as:

- Cloud secret manager
- Encrypted secrets vault
- Hardware-backed key management where available
- Application-level envelope encryption as a minimum fallback

The report must recommend the preferred method for the POC and for production.

### OAuth Setup Wizard

Design a guided setup process:

1. Create connection profile.
2. Select Zoho data centre.
3. Enter or reference the OAuth Client ID.
4. Enter or reference the OAuth Client Secret.
5. Confirm the registered Redirect URI.
6. Select required functional modules.
7. Generate the required OAuth scope list.
8. Redirect the administrator to Zoho consent.
9. Receive the authorisation code securely.
10. Exchange the authorisation code for access and refresh tokens.
11. Store only encrypted secret references.
12. Call `GET /organizations`.
13. Display the accessible Zoho ERP organisations.
14. Select and map the intended organisation.
15. Test master-data APIs.
16. Test procurement APIs.
17. Test finance and fixed-asset APIs.
18. Validate required custom fields and mappings.
19. Run a controlled initial sync.
20. Display a setup completion report.

The administrator should not be expected to manually paste a short-lived access token during normal production setup.

Manual token input may be provided only as a clearly marked development or diagnostic facility.

### Data-Centre Configuration

The connector must not hardcode one global domain.

The setup design must support a configurable data-centre matrix, including:

- Zoho Accounts domain
- Zoho API domain
- Zoho ERP web domain
- Redirect URI
- Region
- Data residency note
- Connection-test endpoint

For the India deployment, the current official documentation should be verified for domains such as:

- `https://accounts.zoho.in`
- `https://www.zohoapis.in`
- Zoho ERP API root ending in `/erp/v3`

The research agent must verify all regional domains from current official documentation before finalising the report.

### Scope Management

Create a scope-management screen that displays:

- Functional module
- Required operation
- Required scope
- Granted scope
- Missing scope
- Business impact
- Remediation action

Scopes must follow least privilege.

Do not request broad `ALL` scopes when narrower READ, CREATE, UPDATE or DELETE scopes are sufficient.

The application must be able to detect when:

- A required scope is missing
- A token has been revoked
- Consent has changed
- An administrator has connected the wrong Zoho organisation
- A connector user no longer has access to a required module

### Organisation Discovery and Mapping

After successful OAuth connection:

- Call the Zoho ERP organisation-list API.
- Display all accessible organisations.
- Require the administrator to select the correct organisation.
- Record the selected `organization_id`.
- Retrieve and display organisation metadata.
- Prevent silent reassignment to a different organisation.
- Require approval or an elevated permission to change organisation mapping.
- Record every mapping change in the audit trail.

Support one application tenant connected to:

- One Zoho ERP organisation
- Multiple Zoho ERP organisations
- Multiple legal entities
- Multiple plants or locations

The report must recommend whether a connection profile should be maintained per organisation, per legal entity or per environment.

### Module Connectivity Test

The setup module must provide a test console that checks:

- Authentication
- Organisation access
- API base URL
- Required scopes
- Rate-limit response
- Pagination
- Contacts or vendors
- Users
- Departments
- Items
- Locations
- Projects
- Taxes
- Currencies
- Chart of Accounts
- Purchase Orders
- Purchase Receives
- Bills
- Vendor Credits
- Vendor Payments
- Journals
- Fixed Assets
- Reporting Tags
- Custom fields required for CAPEX and WBS integration

For every test, display:

- Test name
- Endpoint
- HTTP method
- Result
- Response time
- Record count
- Error code
- Error message
- Correlation ID
- Tested at
- Tested by

Secrets and complete access tokens must never be shown in logs or on screen.

### Field and Dimension Mapping

The setup module must provide a mapping workbench for:

- Zoho Organisation
- Legal Entity
- Plant
- Location
- Department
- Vendor
- Item
- Service
- Project
- WBS Code
- Budget Head
- CAPEX Code
- CWIP GL
- Asset Category
- Fixed Asset Type
- Tax
- Currency
- Exchange Rate
- Chart of Account
- Reporting Tag
- Custom Field
- Purchase Order line
- Purchase Receive line
- Vendor Bill line
- Journal line
- Fixed Asset

For each mapping, store:

- Application object
- Application field
- Zoho ERP module
- Zoho ERP field
- Field API name
- Data type
- Direction
- Mandatory status
- Transformation
- Default value
- Validation
- Source of truth
- Conflict rule
- Effective date
- Version
- Active status

### Sync Configuration

Allow configuration of:

- Direction: inbound, outbound or bidirectional
- Full sync
- Incremental sync
- Manual sync
- Scheduled sync
- Polling frequency
- Look-back window
- Pagination size
- Date filter
- Status filter
- Batch size
- Retry count
- Retry delay
- Exponential backoff
- Dead-letter threshold
- Reconciliation frequency
- Notification recipients
- Automatic disable threshold
- Maintenance window

### Connector Health Dashboard

Create an operational dashboard containing:

- Connection status
- Token status
- Last token refresh
- Granted scopes
- Connected organisation
- API calls used
- Rate-limit warnings
- Last successful sync
- Last failed sync
- Records received
- Records sent
- Records rejected
- Records pending
- Dead-letter count
- Reconciliation differences
- API response-time trend
- Module-wise errors
- Credential-expiry or revocation alerts
- Configuration-change alerts

### Setup Security Controls

The report must define:

- Who may create a connection
- Who may authorise Zoho access
- Who may view the organisation ID
- Who may rotate credentials
- Who may deactivate a connector
- Who may modify mappings
- Who may force a resync
- Who may replay a failed event
- Who may export connector logs

Mandatory controls include:

- Administrator-only access
- Maker-checker for production connection changes
- Multi-factor authentication
- Secrets masked in the UI
- No secrets in application logs
- No secrets in browser local storage
- No secrets in source code
- No secrets in repository files
- Audit log for credential changes
- Credential rotation
- Revocation procedure
- IP and network controls where practical
- CSRF protection for OAuth callbacks
- OAuth `state` validation
- Secure redirect-URI validation
- Encryption at rest and in transit
- Restricted database access
- Environment separation
- Separate production and non-production credentials

### Setup Module Acceptance Criteria

At minimum:

1. An authorised administrator can connect a Zoho ERP organisation without changing source code.
2. OAuth consent and token exchange are completed through a guided flow.
3. The application can discover available Zoho organisations and store the selected `organization_id`.
4. Secrets are encrypted and never displayed after saving.
5. The application can test every required API module.
6. Missing scopes are clearly identified.
7. Master-data and transaction mappings can be configured from the UI.
8. A full and incremental sync can be initiated.
9. Failed records are visible with retry options.
10. All connection and mapping changes are auditable.
11. The connector can be disabled without deleting historical data.
12. The setup supports at least two Zoho ERP organisations in the POC design.


## Source-of-Truth Matrix

Prepare a source-of-truth matrix for all important objects, including:

- Legal entity
- Plant
- Department
- User
- Vendor
- Item or service
- Tax
- Currency
- Project
- WBS element
- Budget head
- Budget
- Budget revision
- Purchase Request
- Purchase Order
- GRN or Purchase Receipt
- Vendor Bill
- Payment
- Fixed Asset
- GL account
- Cost centre
- Profit centre
- Approval matrix

For every object, specify:

- System of record
- Data owner
- Creation system
- Update system
- Sync direction
- Integration method
- External key
- Reconciliation rule
- Conflict handling
- Failure handling

## Zoho ERP API Research and Integration Blueprint

Study the current Zoho ERP API documentation in depth and prepare an endpoint-by-endpoint integration blueprint.

Use only official Zoho ERP API documentation as the primary source.

Download and examine the current Zoho ERP OpenAPI document where available.

Do not rely on Zoho Books or Zoho Inventory APIs as substitutes unless:

1. The same endpoint is expressly documented for Zoho ERP.
2. The report clearly states why a related-product API is being referenced.
3. The actual Zoho ERP endpoint is separately verified.

### Current Official API Baseline to Revalidate

The following is a research baseline identified from the official Zoho ERP API documentation available when this prompt was prepared.

The executing research agent must revalidate every item against current official documentation and record the verification date.

#### API Foundation

Current documentation indicates:

- REST API architecture
- India API root: `https://www.zohoapis.in/erp/v3`
- OAuth 2.0 authentication
- India Accounts domain: `https://accounts.zoho.in`
- `organization_id` required on organisation-specific API calls
- `GET /organizations` available for organisation discovery
- Access token sent in the `Authorization` header
- Access token validity of approximately one hour
- Refresh token used to generate new access tokens until revoked
- Pagination used by list APIs
- HTTP 429 used for rate-limit breaches

The executing agent must verify:

- Current API domains
- Current token rules
- Current daily and per-minute limits
- Current OAuth scope names
- Current pagination defaults and maximums
- Current supported data centres
- Current error model
- Whether a sandbox or test organisation is available
- Whether webhooks or outbound event subscriptions are officially available

Do not assume webhook availability. The currently surfaced API index does not by itself confirm a general webhook framework.

### Mandatory Zoho ERP API Inventory

Prepare a table with at least:

- Business object
- Zoho ERP module
- Official documentation reference
- API base path
- Endpoint
- HTTP method
- Required OAuth scope
- Required parameters
- Required body fields
- Response identifiers
- Pagination support
- Filter support
- Custom-field support
- Approval action support
- Attachment support
- Last-modified field
- Status field
- Direction
- Trigger
- Sync frequency
- Idempotency method
- Reconciliation method
- POC usage
- Production usage
- Verified date
- Verification status
- Limitation

### Minimum Endpoint Families to Study

The report must verify and document, at a minimum, the following current endpoint families.

#### Organisation and Access

- `GET /organizations`
- Organisation detail endpoints
- Users API
- Departments API
- Employees API, where relevant
- Current-user API

Use these for:

- Organisation discovery
- User mapping
- Approval-user mapping
- Department mapping
- Access validation

#### Vendors and Contacts

- `GET /contacts`
- `GET /contacts/{contact_id}`
- Contact create and update endpoints if outbound master creation is recommended
- Contact activation and inactivation
- Contact-person APIs

Use `contact_type=vendor` or the officially supported equivalent where required.

#### Items and Services

- `GET /items`
- `GET /items/{item_id}`
- Bulk item-detail endpoint such as `GET /itemdetails`
- Item create and update endpoints where justified

Use these for PO-line, GRN-line and bill-line mapping.

#### Locations and Plants

- `GET /locations`
- `GET /locations/{location_id}`, if available
- Location create and update endpoints where required
- Location activation status

Study how Zoho ERP locations should map to:

- Legal entity
- Plant
- Warehouse
- Project location
- Delivery location

Do not assume that one Zoho location equals one manufacturing plant.

#### Projects and Tasks

- `GET /projects`
- `GET /projects/{project_id}`
- Project create and update endpoints
- Project activation and inactivation
- Project-task endpoints

Evaluate whether Zoho ERP Projects should be:

- A downstream representation of the external WBS application
- A source of selected project information
- Unused for core WBS control
- Used only for time and service-cost capture

#### Taxes

- `GET /settings/taxes`
- `GET /settings/taxes/{tax_id}`
- Tax-group endpoints
- Tax-transaction reference endpoints where relevant

Use these to determine:

- Recoverable tax
- Non-creditable tax
- TDS or TCS relevance
- GST treatment
- Tax component included in capitalised cost

Tax accounting treatment must remain subject to client policy and professional validation.

#### Currency and Exchange Rates

- `GET /settings/currencies`
- Currency detail endpoint
- Exchange-rate list endpoint
- Exchange-rate detail endpoint

Use these for:

- PO currency
- Project base currency
- Commitment conversion
- Actual conversion
- Exchange-rate variance
- Reporting currency

#### Chart of Accounts

- `GET /chartofaccounts`
- `GET /chartofaccounts/{account_id}`
- Account-status endpoints
- Account-transaction endpoints where relevant

Use these to map:

- CWIP GL
- Capital advance
- Capital creditor
- Freight and duty
- Input tax
- Retention
- Fixed asset accounts
- Project write-off accounts

#### Reporting Tags and Dimensions

Study the official Reporting Tags API and the use of tags or custom fields at:

- Header level
- Line-item level
- Purchase Order
- Vendor Bill
- Journal
- Fixed Asset
- Other relevant purchase transactions

Determine the best supported location for:

- CAPEX Project Code
- WBS Code
- Budget Head
- Plant
- Department
- Asset Category

Do not assume that all tags or custom fields are available on all modules or at line level.

#### Purchase Orders

Verify and document:

- `POST /purchaseorders`
- `GET /purchaseorders`
- `GET /purchaseorders/{purchaseorder_id}`
- `PUT /purchaseorders/{purchaseorder_id}`
- Purchase Order update using unique custom field, where supported
- Custom-field update
- Submit for approval
- Approve
- Reject
- Mark as open
- Mark as billed
- Cancel
- Comments and history
- Attachments

The integration design must determine whether the WBS application will:

- Read approved POs only
- Validate draft POs before approval
- Create POs after external approval
- Update a CAPEX or WBS reference on the PO
- Block or flag over-budget POs
- Receive PO status changes by polling or event
- Reconcile line-level quantities and values

PO integration must be line-level.

The report must identify all relevant IDs, including:

- `purchaseorder_id`
- `line_item_id`
- `vendor_id`
- `item_id`
- `location_id`
- `currency_id`
- `project_id`
- Custom-field identifiers
- Reporting-tag identifiers

#### Purchase Receives or GRN

Verify and document:

- `POST /purchasereceives`
- `GET /purchasereceives/{purchasereceive_id}`
- `PUT /purchasereceives/{purchasereceive_id}`
- `DELETE /purchasereceives/{purchasereceive_id}`
- Whether a list or search endpoint is currently available
- Whether status history is available
- Whether attachments and comments are available
- Whether line-level PO links are returned

Current documentation indicates that Purchase Receive creation requires a `purchaseorder_id`. Revalidate this rule.

The design must handle:

- Partial receipt
- Multiple receipts against one PO
- Receipt reversal or deletion
- Received but unbilled exposure
- Quantity and value reconciliation
- Service procurement where a GRN may not apply

#### Vendor Bills

Verify and document:

- `POST /bills`
- `GET /bills`
- `GET /bills/{bill_id}`
- `PUT /bills/{bill_id}`
- Bill update by unique custom field, where supported
- Custom-field update
- Void
- Reopen
- Submit for approval
- Approve
- Bill payments
- Apply credits
- Attachments
- Comments and history
- PO-to-Bill payload or conversion support

Identify:

- `bill_id`
- `purchaseorder_ids`
- Bill line IDs
- PO line references
- Vendor
- Location
- Currency
- Exchange rate
- Tax
- Account
- Project
- Custom fields
- Reporting tags
- Status
- Balance
- Created time
- Last modified time

The integration must support commitment-to-actual conversion without double counting.

#### Vendor Credits

Study:

- Vendor-credit create, list, detail and update
- Void or status actions
- Approval actions
- Application of vendor credit to a bill
- Refunds
- Comments and history

Use vendor credits to reverse or reduce actual CWIP and restore budget availability where appropriate.

#### Vendor Payments and Advances

Study:

- `POST /vendorpayments`
- `GET /vendorpayments`
- `GET /vendorpayments/{payment_id}`
- Vendor-payment update and delete
- Refund endpoints
- Application to bills
- Excess or advance payment treatment

Determine whether vendor advances affect:

- Budget exposure
- Cash-flow reporting
- Commitment
- Actual CWIP
- Capital advance account

Do not automatically classify a payment as CWIP actual unless accounting policy and source transaction support it.

#### Expenses

Study the Expenses APIs where employee or direct project expenditure may be capitalisable.

Determine whether approved expenses should:

- Increase actual CWIP
- Remain outside project cost
- Be reclassified by journal
- Require CAPEX and WBS coding

#### Journals

Verify and document:

- `POST /journals`
- Journal list and detail
- Update
- Publish
- Reverse
- Submit for approval
- Approve
- Reject
- Comments
- Attachments

Use journals for controlled scenarios such as:

- CWIP adjustment
- Common-cost allocation
- Pre-operative expense allocation
- Capitalisation transfer
- Reversal
- Abandoned-project write-off

The application must not create journals automatically in production without an approved control design.

#### Fixed Assets

Verify and document:

- `POST /fixedassets`
- `GET /fixedassets`
- `GET /fixedassets/{fixed_asset_id}`
- `PUT /fixedassets/{fixed_asset_id}`
- Fixed-asset type APIs
- Status changes
- History
- Forecast depreciation
- Write-off or sale, where relevant
- Comments

Use these for:

- Capitalisation request
- Asset creation
- Componentisation
- Multiple assets from one WBS
- Multiple WBS elements into one asset
- Asset-type mapping
- Capitalisation reconciliation

#### Purchase Request

The current surfaced Zoho ERP API index must be checked carefully for a public Purchase Request API.

At the time this prompt was prepared, a Purchase Request endpoint was not evident in the surfaced public API index.

Therefore, the final report must not assume that a Purchase Request API exists.

The research agent must:

1. Search the current official API documentation.
2. Search the downloadable OpenAPI specification.
3. Check official Zoho ERP help or developer documentation.
4. Record whether public Create, Read, Update, Approval and Status APIs exist.
5. Record whether custom functions or workflow callbacks are supported.
6. Record whether webhooks are supported.
7. Record any licence or edition restrictions.

If no supported Purchase Request API exists, compare these alternatives:

- Purchase Request owned by the external WBS application
- Approved request converted into a Zoho ERP PO
- Controlled CSV or import bridge
- Polling or middleware
- Custom function or vendor-supported extension
- Manual POC simulation
- Deferred integration

### Bidirectional Data-Flow Design

Prepare a definitive data-flow matrix.

For every object, define:

- Source system
- Target system
- Direction
- Create authority
- Update authority
- Delete authority
- Trigger
- Endpoint
- External ID
- Idempotency key
- Sync status
- Reconciliation
- Error owner

At minimum, evaluate the following proposed flows.

#### Zoho ERP to CAPEX & WBS Control Hub

Potential inbound flows:

- Organisations
- Users
- Departments
- Vendors
- Items and services
- Locations
- Taxes
- Currencies and exchange rates
- Chart of Accounts
- Reporting Tags
- Projects, if used
- Purchase Orders
- Purchase Order amendments
- PO cancellations and closures
- Purchase Receives
- Vendor Bills
- Bill reversals
- Vendor Credits
- Vendor Payments
- Expenses
- Journals
- Fixed Assets

#### CAPEX & WBS Control Hub to Zoho ERP

Potential outbound flows:

- Approved project or project reference
- CAPEX Code
- WBS Code
- Budget Head
- Custom-field values
- Reporting-tag values
- Approved Purchase Request output
- Purchase Order creation request, if chosen
- Budget-validation result or approval reference
- Capitalisation instruction
- Journal proposal
- Fixed-asset creation request
- Attachment or approval reference
- Reconciliation status, where supported

The final architecture must clearly identify which outbound actions are:

- Automated
- Approval-gated
- Manual
- POC-only
- Not recommended

### API Usage Rules

The integration design must include:

- OAuth token refresh
- Token cache
- Secure secret reference
- Correlation ID
- Request ID
- Idempotency key
- Retry with exponential backoff
- Handling of HTTP 429
- Pagination
- Incremental filtering
- Look-back window
- Duplicate detection
- Out-of-order event handling
- Last-modified timestamp
- Soft deletion
- Status transition validation
- Dead-letter queue
- Replay
- Reconciliation
- Alerting
- Circuit breaker
- Per-organisation throttling

The current official limits must be verified. The baseline documentation reviewed while preparing this prompt indicated a per-organisation per-minute limit and plan-based daily limits, but these values must not be treated as permanent.

### Custom-Field Strategy

Study whether Zoho ERP permits the required CAPEX and WBS values at header and line-item levels.

Prepare a matrix for:

- CAPEX Project Code
- WBS Code
- Budget Head
- Plant
- Department
- Asset Category
- External Transaction ID
- Budget Check ID
- Approval Reference
- Capitalisation Request ID

For each field, define:

- Zoho module
- Header or line
- Field API name
- Field type
- Mandatory rule
- Unique-value rule
- Read support
- Write support
- Search support
- Upsert support
- Display position
- Reporting impact

The design should prefer stable server-generated Zoho IDs plus controlled external unique IDs.

### Integration Reconciliation

Design reconciliation for:

- Project master
- WBS reference
- Vendor
- Item
- PO header
- PO line
- PO amendment
- PO status
- GRN header
- GRN line
- Bill header
- Bill line
- Credit
- Payment
- Journal
- Fixed Asset
- CWIP GL

For each reconciliation, specify:

- Key
- Source amount
- Target amount
- Quantity
- Currency
- Exchange rate
- Status
- Last modified
- Tolerance
- Exception type
- Correction owner

### API Documentation Deliverables

The final report must include:

1. Zoho ERP API capability matrix
2. OAuth and scope matrix
3. Data-centre matrix
4. Endpoint inventory
5. Request and response field mapping
6. Master-data flow matrix
7. Transaction flow matrix
8. Custom-field and reporting-tag matrix
9. Rate-limit and pagination strategy
10. Error-code handling matrix
11. Retry and dead-letter design
12. Reconciliation matrix
13. Sequence diagrams
14. Sample payloads
15. Setup wizard specification
16. Connector security specification
17. POC mock strategy
18. API test checklist
19. Verified limitations
20. Open questions for Zoho

Where an API, field, event or scope cannot be confirmed from official documentation, mark it:

`UNVERIFIED - REQUIRES ZOHO CONFIRMATION`

Do not fabricate an endpoint or infer one from another Zoho product.

## Functional Modules to Design

The proposed application should be analysed under the following modules.


### 1. Zoho ERP Integration Setup and Connector Administration

- Connection-profile master
- Multi-organisation support
- Multi-environment support
- Zoho data-centre selection
- OAuth Client ID reference
- OAuth Client Secret reference
- Redirect URI
- OAuth consent
- Access and refresh token management
- Organisation discovery
- Organisation selection
- Scope validation
- API connectivity tests
- Module-level connectivity tests
- Secret rotation
- Connector enable and disable
- Initial sync
- Incremental sync
- Manual resync
- Sync scheduling
- Polling configuration
- Field mapping
- Custom-field mapping
- Reporting-tag mapping
- External-ID mapping
- Data-direction configuration
- API usage monitoring
- Rate-limit monitoring
- Retry queue
- Dead-letter queue
- Reconciliation
- Connection audit trail
- Configuration versioning


### 2. Organisation and Master Data

- Legal entity
- Plant
- Department
- Cost centre
- Profit centre
- Project type
- Asset category
- Budget head
- Approval matrix
- Currency
- Tax treatment
- User and role

### 3. CAPEX Project Master

- Project code
- Project name
- Description
- Entity
- Plant
- Department
- Project sponsor
- Project owner
- Start date
- Planned completion date
- Project type
- Asset category
- CWIP GL
- Project status
- Original budget
- Current approved budget
- Zoho ERP reference

### 4. WBS Structure

- Unlimited or configurable hierarchy
- Parent-child relationship
- WBS code
- WBS description
- WBS type
- Responsible owner
- Budget head
- Planned dates
- Actual dates
- Progress percentage
- Status
- Asset category
- Settlement receiver
- Allow procurement flag
- Allow posting flag

### 5. Budget Management

- Original budget
- Budget by WBS
- Budget by budget head
- Budget period
- Revision
- Supplement
- Return
- Transfer
- Release
- Freeze
- Versioning
- Approval
- Available budget
- Forecast at completion

### 6. Procurement Control

- PR validation
- PR reservation, if adopted
- PO validation
- PO commitment
- Amendment revalidation
- Cancellation release
- Closure release
- Partial receipt
- Partial billing
- Service procurement
- Material procurement
- Contract and work order
- Retention and advance
- Change order

### 7. CWIP Accounting Control

- Actual CWIP feed from vendor bills
- Labour cost
- Material consumption
- Freight and duty
- Non-creditable tax
- Journal adjustment
- Reversal
- Credit note
- Currency variance
- Allocation of common cost
- Reconciliation to CWIP GL

### 8. Project Completion and Capitalisation

- Technical completion
- Financial completion
- Open commitment review
- Pending bill review
- Settlement proposal
- Asset creation request
- Componentisation
- Allocation
- Capitalisation approval
- Transfer from CWIP
- Project close
- Reopen control

### 9. Reporting and Alerts

- Budget versus commitment versus actual versus available
- Original versus revised budget
- Exposure percentage
- Project-level view
- WBS-level view
- Plant-level view
- Department-level view
- Budget-head-level view
- Open PO ageing
- Pending GRN
- Pending invoice
- CWIP ageing
- Projects awaiting capitalisation
- Budget exception
- Budget revision history
- Audit trail
- Reconciliation report
- Data sync exception report

## Roles and Approval Matrix

Propose a role model including at least:

- Requestor
- Project Manager
- Plant Head
- Department Head
- Procurement
- Finance
- Project Finance Controller
- CAPEX Committee
- CFO
- Management Approver
- System Administrator
- Internal Auditor
- Read-only Management User

For every major transaction, prepare a RACI and approval matrix.

Include amount-based, plant-based, entity-based, department-based and exception-based approval rules.

## Detailed Business Rules

Define precise business rules for:

- Budget availability
- Commitment calculation
- Actual calculation
- Assigned value
- Exposure percentage
- Budget utilisation
- PR reservation
- PO commitment
- Tax treatment
- Foreign currency
- Partial billing
- Advance payment
- Retention
- Cancellation
- Closure
- Reversal
- Transfer of budget
- Budget-head reclassification
- WBS reclassification
- Reopening of closed project
- Capitalisation
- Abandoned project
- Data reconciliation

Provide formulas and worked examples.

## Data Model

Prepare an implementation-ready logical data model.

Include:

- Entity list
- Field list
- Data type
- Mandatory flag
- Unique key
- Foreign key
- Status field
- Audit field
- Source system ID
- Sync status
- Version field
- Effective date
- Soft-delete flag

At minimum, define entities for:

- Organisation
- Plant
- Department
- Project
- WBS Element
- Budget Head
- Budget Version
- Budget Line
- Budget Revision
- Approval Matrix
- Approval Instance
- Purchase Request Reference
- Purchase Order Reference
- PO Line
- GRN Reference
- GRN Line
- Vendor Bill Reference
- Bill Line
- Commitment Ledger
- Actual Ledger
- Budget Ledger
- Capitalisation Request
- Asset Allocation
- Zoho Connection Profile
- OAuth Credential Secret Reference
- OAuth Scope Requirement
- Zoho Organisation Mapping
- API Endpoint Catalogue
- Field Mapping
- Transformation Rule
- Sync Configuration
- Sync Cursor
- Integration Event
- Integration Request
- Integration Response
- Sync Log
- Retry Queue
- Dead-Letter Record
- API Usage Record
- Reconciliation Log
- Connector Audit Log
- Audit Log
- Attachment
- Comment

Provide an ER diagram using Mermaid.

## State Machines

Create Mermaid state diagrams for:

- Project lifecycle
- WBS lifecycle
- Budget lifecycle
- Budget revision lifecycle
- Purchase commitment lifecycle
- Capitalisation lifecycle
- Integration event lifecycle

## API and Integration Blueprint

Define a preliminary API contract for the POC.

Include:

- Authentication
- Authorisation
- API versioning
- Idempotency
- Pagination
- Filtering
- Sorting
- Error model
- Correlation ID
- Audit metadata
- Webhook security
- Retry
- Dead-letter handling

Prepare sample endpoints such as:

- `POST /projects`
- `POST /projects/{projectId}/wbs`
- `POST /projects/{projectId}/budgets`
- `POST /budget-check`
- `POST /budget-revisions`
- `POST /integrations/zoho/purchase-orders/sync`
- `POST /integrations/zoho/grns/sync`
- `POST /integrations/zoho/vendor-bills/sync`
- `POST /capitalisation-requests`
- `GET /reports/budget-exposure`
- `GET /reconciliation/cwip`

Provide sample request and response payloads.

## Accounting Design

Prepare an accounting design note covering:

- CWIP GL structure
- Project and WBS dimensions
- Budget heads
- Vendor bill posting
- Recoverable and non-recoverable tax
- Freight and landed cost
- Advance to vendor
- Retention payable
- Capital creditor
- Asset Under Construction
- Transfer to fixed asset
- Capitalisation journal
- Reversal
- Write-off of abandoned project
- Reconciliation between subledger and general ledger

Do not give legal or tax conclusions without identifying jurisdiction-specific validation requirements.

## Security and Internal Control Requirements

Include:

- Role-based access control
- Segregation of duties
- Maker-checker
- Least privilege
- Multi-factor authentication
- Encryption in transit and at rest
- Secret management
- API credential rotation
- Session management
- Audit logging
- Immutable budget history
- Immutable approval history
- Field-level access
- Entity and plant-level data restriction
- Data export control
- Backup
- Disaster recovery
- Vulnerability management
- OWASP controls
- Dependency scanning
- Secure coding
- Rate limiting
- Input validation
- File upload security
- Privacy and data retention
- DPDP Act considerations for personal data
- Audit evidence retention

## Non-Functional Requirements

Define measurable requirements for:

- Performance
- Scalability
- Availability
- Reliability
- Data integrity
- Concurrency control
- Optimistic locking
- High-value transaction accuracy
- Integration recovery
- Reconciliation
- Observability
- Logging
- Monitoring
- Alerting
- Backup
- Restore
- RPO
- RTO
- Maintainability
- Portability
- Configurability
- Auditability
- Browser support
- Accessibility
- Mobile responsiveness

## POC Scope

The POC must prove the complete control loop, not merely display screens.

At minimum, the POC should demonstrate:

1. Create a manufacturing CAPEX project.
2. Create a three-level WBS.
3. Define budget heads and approved budgets.
4. Upload or create an original budget.
5. Submit a Purchase Request.
6. Perform a budget availability check.
7. Approve a within-budget transaction.
8. Route an over-budget transaction for exception or revision.
9. Create or simulate a Zoho ERP Purchase Order.
10. Record PO commitment.
11. Process partial GRN.
12. Process partial vendor bill.
13. Reduce commitment and increase actual without double counting.
14. Amend a PO upward and revalidate budget.
15. Cancel or close a PO and release unused commitment.
16. Process a budget revision with version history.
17. Show project, WBS and budget-head dashboards.
18. Demonstrate project completion review.
19. Create a capitalisation request.
20. Allocate CWIP to one or more fixed assets.
21. Show the audit trail.
22. Show Zoho integration and reconciliation logs.
23. Configure a Zoho ERP connection profile through the setup wizard.
24. Complete OAuth authorisation or demonstrate the full flow using a controlled mock where live credentials are unavailable.
25. Discover and select a Zoho ERP organisation.
26. Validate required scopes and display missing scopes.
27. Run module-level connectivity tests.
28. Configure at least one master-data mapping and one transaction-line mapping.
29. Perform an initial vendor, item, location and Chart of Accounts sync.
30. Pull or simulate an approved Purchase Order from Zoho ERP.
31. Pull or simulate a Purchase Receive and Vendor Bill.
32. Push or simulate a controlled outbound CAPEX/WBS reference.
33. Demonstrate token refresh.
34. Demonstrate rate-limit handling.
35. Demonstrate retry and dead-letter processing.
36. Demonstrate line-level reconciliation between PO, receipt and bill.

## POC Data Set

Create a sample data set for a new manufacturing plant.

Illustrative project:

`New Manufacturing Plant - Phase 1`

Illustrative WBS:

- Land and site development
- Civil and structural work
- Plant and machinery
- Electrical
- Utilities
- Instrumentation and automation
- Installation
- Testing and commissioning
- Consultancy and project management
- Pre-operative expenses
- Contingency

Use realistic Indian currency examples and at least:

- 2 entities
- 2 plants
- 3 projects
- 3 WBS levels
- 10 budget heads
- 20 Purchase Requests
- 15 Purchase Orders
- Partial GRNs
- Partial bills
- PO amendment
- PO cancellation
- Budget supplement
- Budget transfer
- One overrun
- One capitalisation case
- One abandoned or cancelled WBS case

## Test Scenarios and Acceptance Criteria

Prepare:

- Functional test cases
- Negative test cases
- Integration test cases
- Reconciliation test cases
- Security test cases
- Concurrency test cases
- Approval test cases
- Audit trail test cases
- Performance test cases
- Capitalisation test cases

Every POC user story must have clear acceptance criteria.

Include edge cases such as:

- Two users attempting to consume the same remaining budget simultaneously
- Duplicate webhook
- Out-of-order events
- Failed sync after PO approval
- Bill received before GRN
- Bill amount greater than PO
- PO currency different from project currency
- Backdated transaction
- Reopened project
- Deleted or deactivated budget head
- Vendor bill reversal
- PO line mapped to the wrong WBS
- Budget transfer after commitment
- Capitalisation with pending commitment

## Recommended Technology Stack

Evaluate and recommend a POC technology stack.

Compare at least:

- Frontend
- Backend
- Database
- Authentication
- Queue or event processing
- Caching
- Reporting
- File storage
- Containerisation
- Deployment
- CI/CD
- Logging
- Monitoring
- Testing

The recommendation should prioritise:

- Speed of POC development
- Enterprise scalability
- Security
- Ease of Zoho API integration
- Maintainability
- Low vendor lock-in
- Cost
- Availability of developer skills

Do not start coding.

## Implementation Roadmap

Prepare a phased roadmap:

- Phase 0: Discovery and validation
- Phase 1: POC
- Phase 2: Pilot
- Phase 3: Production rollout
- Phase 4: Advanced project controls
- Phase 5: Multi-ERP or group-wide rollout

For each phase include:

- Scope
- Deliverables
- Dependencies
- Estimated effort range
- Key roles
- Risks
- Exit criteria

Do not fabricate precise commercial estimates. Use effort bands and state assumptions.

## Risks and Open Questions

Prepare a risk register covering:

- Zoho ERP API limitations
- Missing Purchase Request API
- Missing or limited webhook support
- Incorrect OAuth scopes
- Wrong Zoho organisation mapping
- Token revocation
- Refresh-token rotation failure
- API daily-limit exhaustion
- Per-minute throttling
- Pagination defects
- Incomplete custom-field support
- Header versus line-level mapping limitations
- Delayed or missing events
- Data duplication
- Budget race conditions
- Incorrect commitment release
- Accounting mismatch
- User adoption
- Approval delays
- Master-data quality
- Capitalisation policy
- Multi-entity complexity
- Tax treatment
- Foreign currency
- Migration of existing CWIP
- Opening commitments
- Audit requirements
- Cybersecurity
- Supportability

Prepare a separate client clarification questionnaire.

## Required Final Deliverable

Create one Markdown file named:

`SAP_WBS_Zoho_ERP_POC_Blueprint.md`

The report must contain, at minimum:

1. Document Control
2. Executive Summary
3. Client Requirement Summary
4. Requirement Traceability Matrix
5. SAP PS and WBS Research
6. SAP Capability Catalogue
7. Manufacturing Plant CAPEX Use Cases
8. Current Requirement versus SAP WBS Mapping
9. Gap Analysis
10. Target Operating Model
11. Architecture Options
12. Recommended Architecture
13. Source-of-Truth Matrix
14. Zoho ERP Connector Setup Module
15. OAuth, Credential and Scope Management
16. Zoho ERP API Capability Matrix
17. Zoho ERP Endpoint Inventory
18. Bidirectional Data-Flow Matrix
19. Field, Custom-Field and Reporting-Tag Mapping
20. Functional Modules
21. Detailed Business Rules
22. Budget and Commitment Calculation Logic
23. Accounting Design
24. Zoho ERP Integration Assessment
25. API and Event Architecture
26. Integration Security and Secret Management
27. Integration Reconciliation Design
28. Data Model
29. ER Diagram
30. State Diagrams
31. Role and Approval Matrix
32. Security and Internal Controls
33. Non-Functional Requirements
34. Reporting and Dashboard Catalogue
35. POC Scope
36. User Stories
37. Acceptance Criteria
38. Sample Data
39. Test Scenarios
40. Deployment Recommendation
41. Implementation Roadmap
42. Risk Register
43. Assumptions
44. Open Questions
45. Client Clarification Questionnaire
46. Source References
47. SAP-Familiar UI/UX Design Blueprint
48. Screen Catalogue and Navigation Map
49. Design Tokens and Component Catalogue
50. UI Acceptance Criteria
51. Handover Instructions for the Coding Agent

## Handover Instructions for the Coding Agent

The final section must instruct the future coding agent how to use the blueprint.

It must clearly state:

- Which sections are binding requirements
- Which items are assumptions
- Which items require confirmation
- POC scope
- Out-of-scope items
- Recommended repository structure
- Suggested implementation sequence
- Required environment variables
- Required Zoho credentials or sandbox dependencies
- Zoho data-centre configuration
- OAuth client registration steps
- Required scopes
- Secret-manager configuration
- Organisation discovery and mapping
- Required custom fields and reporting tags
- Endpoint inventory
- Sync direction for each object
- Initial-sync procedure
- Incremental-sync procedure
- Token-refresh procedure
- Rate-limit handling
- Retry and dead-letter handling
- Reconciliation procedure
- Mocking strategy when Zoho APIs are unavailable
- Definition of done
- Mandatory tests
- Security checks
- Documentation requirements
- Demo script

## Output Quality Rules

- Produce a professional enterprise solution document.
- Be specific and implementation-ready.
- Avoid generic statements.
- Use tables wherever comparison is required.
- Use Mermaid for architecture, workflow, state and ER diagrams.
- Include formulas and worked examples.
- Include requirement IDs and traceability.
- Include source references for SAP and Zoho claims.
- Mark assumptions clearly.
- Mark unverified API capabilities clearly.
- Cite the official Zoho ERP API documentation for every confirmed endpoint and scope.
- Record the API documentation verification date.
- Do not use Zoho Books or Zoho Inventory endpoints as silent substitutes for Zoho ERP.
- Do not fabricate an endpoint, scope, field, webhook or event.
- Where an API is absent, specify a controlled fallback.
- Do not fabricate product functionality.
- Do not write application code in this phase.
- Pseudocode, schemas, API contracts and sample payloads are allowed.
- Do not overwrite the original requirement document.
- Save the complete output as `SAP_WBS_Zoho_ERP_POC_Blueprint.md`.

## Definition of Done

The task is complete only when:

1. The attached requirement document has been fully analysed.
2. SAP WBS and SAP Project System have been researched from authoritative sources.
3. Every requirement has been mapped to SAP capability, target application capability and Zoho ERP integration.
4. Budget, commitment, actual and available-budget logic is unambiguous.
5. Double counting is prevented in all identified transaction states.
6. The data model and integration design are implementation-ready.
7. The POC scope demonstrates the full business control cycle.
8. User stories and acceptance criteria are complete.
9. Risks, assumptions and open questions are clearly identified.
10. The Markdown report can be handed directly to Codex or Claude Code for POC development.
11. The UI/UX blueprint is sufficiently detailed to produce a consistent SAP-familiar interface.
12. The recommended frontend library and all SAP-oriented resources have been reviewed for current licensing suitability.
13. The design does not copy SAP branding, logos, proprietary assets or exact copyrighted screens.
14. UI acceptance criteria, accessibility requirements and visual regression expectations are complete.
15. A dedicated Zoho ERP setup module has been completely specified.
16. OAuth, data-centre, organisation-discovery and credential-management requirements are implementation-ready.
17. Every required Zoho ERP object has an endpoint and scope assessment.
18. Bidirectional data flows are defined at header and line-item levels.
19. Purchase Request API availability has been explicitly verified or a controlled fallback has been designed.
20. Token refresh, pagination, rate limits, retries, dead-letter handling and reconciliation are fully specified.
21. Custom fields and reporting tags required for CAPEX, WBS and Budget Head references have been verified.
22. The POC can demonstrate connection setup, initial sync, incremental sync, failure recovery and reconciliation.
