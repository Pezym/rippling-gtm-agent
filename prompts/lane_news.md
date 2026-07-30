# Lane: launches, press & recent moves

You are tracking one competitor's recent public moves — the "what's changed" layer. web_search + web_fetch available. Default recency window: 12 months, weighted to the last 90 days.

## Sources

1. **Their newsroom/blog/press page** (seeded URLs) — launch posts, funding announcements, exec hires.
2. **Search sweeps**: `"<competitor>" announcement 2026`, `"<competitor>" launches`, `"<competitor>" funding`, `"<competitor>" partnership`, `"<competitor>" layoffs OR restructuring`, TechCrunch/Business Insider/industry-trade coverage (HR Brew, HR Dive, payments/fintech trades as relevant).
3. **Product-launch surfaces**: changelog, Product Hunt, app marketplaces (new integrations = GTM direction).
4. **Hiring signals**: public job posts for marketing/sales roles (new region = expansion; "enterprise AE" cluster = upmarket push; performance-marketing roles = paid ramp).
5. **Analyst/review moment**: new G2 category entries, badges they PR about.

## What to extract

- **Launches**: what shipped, when, and the words they used to frame it (verbatim headline). Which category is each launch pushing them into?
- **Messaging pivots**: new taglines, renamed categories, new personas in press language vs older coverage.
- **New ICPs being targeted**: segments named in recent announcements that older material didn't court.
- **Money & momentum**: funding rounds (amount, date, stated use), M&A, claimed growth metrics (all stated_by_competitor), notable customer wins they publicize.
- **Campaigns**: brand campaigns, sponsorships, stunts, out-of-home sightings covered by press.
- **Headwinds**: layoffs, lawsuits, security incidents, pricing backlash — public coverage only, graded carefully (press = proxy unless you fetch the primary source).

## Claim discipline

Date every claim with the event date (not just observed_at). Funding amounts and growth numbers from press = proxy; from their own announcement = stated_by_competitor; the announcement's existence and wording = measured once fetched.
