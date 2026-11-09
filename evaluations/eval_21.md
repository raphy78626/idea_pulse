# Deep Evaluation — Show HN: Drop a SQL schema, get an interactive ER diagram

*Source: HN Show HN · 15 pts · 5 comments · evaluated 2026-11-02 10:21 · model: nvidia/nemotron-3.5-super-vl:free*

---

## Verdict
SKIP · 3/10 · A free tool in a crowded space with weak early interest and little chance developers will pay.

## Pain & evidence
The Show HN post earned only 15 points and 5 comments. That is a weak signal for a developer tool. People need database diagrams once in a while, not every day. Most will use a free tool and leave. There is no evidence that users will reach for a credit card.

## Target buyer
Who pays: solo developers, tiny dev shops (2–5 people), coding bootcamp students. Who does not pay: big companies (they already have enterprise tools), students with no money.  
Where they hang out: Reddit (r/SQL, r/webdev, r/learnprogramming), LinkedIn, Twitter/X, IndieHackers, Discord programming servers.

## Competition & wedge
| Alternative | Cost | Weakness | Our edge |
|-------------|------|----------|----------|
| dbdiagram.io | Free tier, paid plans | Requires login, slow paste flow | No login, instant diagram |
| draw.io | Free | Generic, not SQL-focused | Built only for SQL schemas |
| SQLDBM | Paid | Expensive, enterprise feel | Cheap, one-person tool |

Wedge is thin: speed and zero signup. Big players can copy this in a weekend.

## Monetization
Pricing: ₹49 one-time ($0.60) to save or share; ₹199/month ($2.40) for private diagrams and image export. First-month revenue range: ₹0 to ₹5,000 ($0 to $60), most likely ₹0. Developers rarely pay for diagramming.

## Distribution (exact 3 channels)
1. Reddit: post a free link in r/SQL and r/webdev with a short GIF demo.  
2. Product Hunt: launch on a Tuesday morning; ask friends for the first five upvotes.  
3. LinkedIn: solo dev posts “I built this in a weekend” and tags small software agency owners in India and the US.

## Risks & kill-criteria
Risk: dbdiagram.io or draw.io adds one-click SQL import. Risk: no repeat usage; user diagrams once and never returns.  
Kill criteria: fewer than 10 signups in 14 days, zero paying users after 30 days, or more than 90 percent of visitors leave without drawing.

## 7-day MVP plan (day-by-day)
Day 1: Write a simple parser for MySQL and PostgreSQL table definitions; turn them into boxes and lines.  
Day 2: Build one web page: paste SQL on the left, see the diagram on the right. Use a free diagram library.  
Day 3: Add zoom, pan, and click-to-highlight a table.  
Day 4: Deploy to Vercel or Netlify; add a .in or .com domain.  
Day 5: Add “download image” and a shareable link.  
Day 6: Post on Reddit, Product Hunt, and LinkedIn the same day.  
Day 7: Count visitors and signups. Email every user asking what they would pay. Decide to continue or kill.

## Final call
Do not build this as a paid product. Build it only as a free portfolio piece if you want to prove you can ship fast.