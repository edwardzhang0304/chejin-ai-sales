# Knowledge Management Design QA

## Comparison Target

- Source visual truth: the user's knowledge-management wireframe in the current task, plus the existing operations-console visual system captured from `http://127.0.0.1:8791/website/ops-admin.html?auth=app&module=vehicles` and `?module=sales`.
- Source screenshots: `/private/tmp/ops-vehicle-reference.png`, `/private/tmp/ops-sales-reference.png`.
- Implementation: `http://127.0.0.1:8791/website/ops-admin.html?auth=app&module=knowledge`.
- Implementation screenshots: `/private/tmp/ops-knowledge-list.png`, `/private/tmp/ops-knowledge-detail.png`.
- Combined comparison evidence: `/private/tmp/knowledge-design-comparison.png`.
- Viewport: 1280 x 720 CSS px.
- Capture dimensions: all source and implementation screenshots are 1280 x 720 px. Browser device pixel ratio reported 2, while the browser capture was normalized to CSS pixel dimensions, so no additional density scaling was applied.
- States compared: vehicle list versus knowledge list; sales detail drawer versus knowledge detail drawer.

## Full-view Comparison

- The module keeps the existing 220 px sidebar, workspace padding, 67 px page header, 110 px metric-card height, 8 px radius, table density, blue primary action and white panel treatment.
- The requested three-card summary intentionally replaces the four-card pattern for this module while preserving the same height, typography and spacing rhythm.
- The list uses the existing no-action-column interaction model. Opening a row reveals a right drawer while retaining a readable four-column list.

## Focused-region Comparison

- The list/filter region and detail drawer were reviewed separately because the table alignment, status chips, long-form rule copy and drawer actions are not legible enough in a single full-view image.
- Typography uses the existing Apple/PingFang/system stack, existing weights and sizes. No negative letter spacing or viewport-scaled text was introduced.
- Colors use the existing `--primary`, `--primary-soft`, `--line`, `--surface-soft`, `--danger` and semantic status tokens.
- No new image assets are required for this data-management module; existing brand assets and controls are unchanged.
- Product copy follows PRD v0.9.61: only `已发布 / 已归档` appear as product states, and new/edit forms expose only `取消 / 发布`.

## Findings

- No remaining P0, P1 or P2 visual issues.
- P3: the version-record drawer currently demonstrates three representative releases rather than a complete paginated history. This is appropriate for the approval prototype and does not block layout or interaction review.

## Comparison History

1. Initial detail-drawer pass found a P2 horizontal overflow: the 360 px knowledge drawer extended 12 px beyond the 1280 px viewport because its late module rule overrode the shared narrow-window width.
2. Fixed by restoring the existing 332 px narrow-window drawer width and matching `minmax(0, 1fr) 332px` grid track at widths below 1440 px.
3. Post-fix evidence measured the drawer right edge at 1264 px with viewport width 1280 px and document scroll width 1280 px. The list remains 680 px wide and all persistent controls are visible.

## Primary Interactions Tested

- Open knowledge detail from a table row.
- Switch detail drawer to edit mode.
- Open the publication validation and before/after difference dialog.
- Open the new-knowledge dialog.
- Open release history and release detail.
- Search to a no-result state and clear the filter.
- Verify no browser console warnings or errors.

## Implementation Checklist

- [x] Navigation entry after vehicle management.
- [x] Three summary cards and clickable current-version card.
- [x] Search, status filter, list, pagination and no-result state.
- [x] Detail and in-drawer edit states.
- [x] New, publish-preview, archive-preview and unsaved-change dialogs.
- [x] Release history, release detail and rollback confirmation entry.
- [x] Narrow-window drawer containment.

final result: passed
