# Spark Your Spotify — v1 Spec

**Status:** Prototype / proof-of-concept
**Facilitated by:** Goyle (demo inspired by SparkFM Online Radio, Boston)
**Target runtime:** Local CLI (Node or Python — implementer's call; see §9)
**Target AI:** Claude Sonnet 4.5 via Anthropic API
**Primary DSP for v1:** Spotify

---

## 1. What this is

A CLI tool that reads a Spotify user's listening data, figures out where they live, finds artists local to their area that they haven't heard, and produces two artifacts:

1. **A 10-song "Spark Playlist"** — a mix of local/regional artists new to the user (discovery) and artists they already love (bridge tracks), designed to onboard them into their own local music scene.
2. **A single-file, self-contained HTML "Spark Card"** — a personalized, Spark-branded summary of the user's taste and why the recommended artists fit it. Screenshot-shareable on Instagram, sendable as a file.

The product is Spark-branded and Spark-voiced, but works for users anywhere — a listener in Brooklyn gets Brooklyn artists, a listener in Atlanta gets Atlanta artists, a listener in Burlington VT gets northern-New-England artists. SparkFM Online Radio is the inspiration and the brand face; the tool serves as a demo of what a local-music-amplification tool can be, with SparkFM credited and linked as the exemplar.

Long-term vision (out of scope for v1): a one-click button inside Spotify. V1 is the CLI that proves the output is good enough that people would push the button.

---

## 2. Goals & non-goals

### Goals
- Prove the core loop: Spotify taste in → local artist recommendations + branded HTML out.
- Output quality good enough that SparkFM would proudly associate their name with it.
- Works for any user in any US location without manual configuration.
- Tight feedback loop — one command, <90 seconds to result.

### Non-goals (v1)
- No web app, no hosting, no auth server beyond Spotify OAuth's required redirect.
- No Tidal, Apple Music, or other DSPs.
- No Spotify playlist creation via API (tracks are listed as deep links only).
- No user accounts, no persistence beyond local files.
- No mobile.
- International users (v1 is US-only; regions table is US-only).

---

## 3. User flow

1. User runs `spark` from terminal.
2. First run: CLI opens browser to Spotify OAuth consent, captures redirect, stores refresh token locally in `~/.spark/token.json`. Subsequent runs reuse the token.
3. CLI detects user's approximate location via IP geolocation, prints it, and asks for confirmation:
   ```
   Detected location: Boston, MA 02118. Is this right? [Y/n/enter zip]
   ```
4. CLI pulls the user's **taste snapshot** from Spotify (§4).
5. CLI builds the **candidate pool** — local artists near the user (§5).
6. CLI calls Claude with taste + candidates (§6).
7. CLI renders `spark-card-<timestamp>.html` (§7) and prints the playlist to stdout.

---

## 4. Taste snapshot — what Claude sees about the user

Pulled from Spotify's Web API. Normalized to:

```jsonc
{
  "user_handle": "goyle",
  "user_location": {
    "city": "Boston",
    "state": "MA",
    "zip": "02118",
    "lat": 42.34,
    "lng": -71.07
  },
  "top_artists_long_term": [   // ~1 year, top 30
    { "name": "SZA", "genres": ["r&b", "neo-soul"], "spotify_id": "..." }
  ],
  "top_artists_medium_term": [ /* ~6mo, top 30 */ ],
  "top_artists_short_term": [ /* ~4wk, top 30 */ ],
  "top_tracks_medium_term": [ /* top 50 */ ],
  "recent_plays": [ /* last 50 */ ],
  "playlist_names": [ /* names only, for vibe signal */ ],
  "genre_distribution": { "r&b": 0.31, "hip-hop": 0.22, ... }
}
```

### Spotify endpoints used
- `GET /v1/me` — display name
- `GET /v1/me/top/artists?time_range={short|medium|long}_term&limit=30`
- `GET /v1/me/top/tracks?time_range=medium_term&limit=50`
- `GET /v1/me/player/recently-played?limit=50`
- `GET /v1/me/playlists?limit=50` (names only — do not fetch tracks)

### OAuth scopes required
- `user-top-read`
- `user-read-recently-played`
- `playlist-read-private`
- `user-read-private`

### Size cap
Total serialized snapshot ≤ 8KB. Truncate top-N if needed.

---

## 5. Candidate pool — finding local artists

This is v1's hardest problem. The pipeline:

### Step 1: Location → geographic scope
- IP geolocation via a free service (ip-api.com or ipapi.co — no auth needed at low volume).
- User can override with a ZIP or city via confirmation prompt or `--zip=02118` flag.
- Resolve to: city, state, metro area, region.

### Step 2: Build seed artist set
Two sources, merged:

**Source A — Spotify related-artists graph from user's top artists.**
For each of the user's top 30 artists, call `GET /v1/artists/{id}/related-artists`. Dedupe. Remove any artist already in the user's top 30 (user already knows them). This yields ~200–500 candidate artists weighted by taste adjacency.

**Source B — Spotify search for local scene.**
Query Spotify search with combinations like `"hip hop" "Boston"` or `"r&b" "Massachusetts"` across the user's top genres. Pull artist results. This surfaces artists that aren't in the user's graph but are relevant to their scene.

### Step 3: Geo-enrichment via MusicBrainz
For each candidate artist, look up their origin on MusicBrainz:
- Endpoint: `https://musicbrainz.org/ws/2/artist?query=artist:{name}&fmt=json`
- Free, no auth, rate limit: 1 req/sec (honor this — cache aggressively).
- Extract `area`, `begin-area`, and `life-span` fields.
- If no MusicBrainz match, drop the candidate. Missing geo data = not usable.

Cache MusicBrainz results locally in `~/.spark/mb_cache.json` with 30-day TTL.

### Step 4: Adaptive radius filtering
Filter candidates by distance from user's location, expanding until pool is healthy:

```
radius_tiers = [
  ("city",     same city or ZIP within ~10mi),
  ("metro",    same metro area / commuter zone ~30mi),
  ("state",    same US state),
  ("region",   same US region — see lookup table)
]

for tier in radius_tiers:
    pool = filter_candidates_by(tier)
    if len(pool) >= 30:
        return pool, tier
# If all tiers exhausted, return whatever region gave us, flagged as sparse
```

The tier name (`"city"`, `"metro"`, `"state"`, `"region"`) is passed through to the HTML renderer so the UI can say *"Artists from Greater Boston"* or *"Artists from New England"* honestly.

### Step 5: Regions lookup
Start with US Census Bureau regions/divisions, then override with cultural music-region coherence where it matters. Store as `data/regions.json`:

```json
{
  "new_england": ["MA", "CT", "RI", "NH", "VT", "ME"],
  "dmv":         ["DC", "MD", "VA"],
  "deep_south":  ["GA", "AL", "MS", "LA"],
  "texas":       ["TX"],
  "pacific_nw":  ["WA", "OR"],
  "bay_area":    ["CA-bay"],
  "socal":       ["CA-south"]
}
```

A state can belong to multiple regions (CA splits into Bay / SoCal). Maintain as data, not code.

### Step 6: Scoring & truncation
Score remaining candidates:
```
score = 0.5 * taste_adjacency        // from Source A graph proximity
      + 0.3 * genre_match_to_user    // cosine similarity of genres
      + 0.2 * spotify_popularity_inverse  // prefer less popular (more discovery)
```
Pass the top 40 into Claude.

### Step 7: Deduplication against user's known artists
Never recommend an artist that appears in the user's top_artists_long_term. Already-known = not discovery.

---

## 6. The Claude prompt

Model: `claude-sonnet-4-5` (or latest Sonnet at build time).

### System prompt

```
You are the curation voice of a local-music discovery tool inspired by
SparkFM Online Radio, a Boston-based digital station that amplifies
underserved voices. Your job: look at a listener's Spotify taste and put
them on to local/regional artists they've never heard, in a way that feels
like a friend with great taste making the intro.

Voice guidelines:
- Warm, observant, confident. 6/10 on the flattery scale — affirming
  without being sycophantic.
- Spark-adjacent register: "pour into," "tap in," "for the culture," light
  emoji use (≤2 per section). Never corporate, never horoscope-y.
- Specific over general. "You've been living in your neo-soul bag" beats
  "You love R&B." Name actual artists from their taste when drawing
  connections.
- Never say "I noticed" or "based on your listening" or "I can see" —
  just state the observation directly.

Calibration:
- TOO COLD: "Your top artists include SZA and Jazmine Sullivan. You may
  enjoy [Local Artist]."
- TOO WARM: "Your taste is absolutely IMPECCABLE and speaks volumes about
  your soul."
- JUST RIGHT: "You've been deep in emotional-weight R&B — SZA, Jazmine,
  Summer Walker. [Local Artist] is your scene's answer. Same vulnerability,
  her own grain."

For each recommended artist, write a 1–2 sentence "why this matches you"
bridge that names at least one artist from the listener's actual top
artists. This is the whole trick — it has to feel earned.

Hard rules:
- Every spotlight artist and every discovery track MUST come from the
  candidate pool. Do not invent artists. Do not recommend artists
  already in the user's top artists list.
- Bridge tracks can be from any artist in the user's top tracks — those
  are by definition familiar.
- Output strictly as JSON matching the schema. No prose outside the JSON.
```

### User prompt structure

```
<taste_snapshot>
{serialized TasteSnapshot}
</taste_snapshot>

<candidate_pool>
{top 40 scored candidates, each with: name, genres, spotify_id, spotify_url,
 origin_city, origin_state, brief_bio_if_available}
</candidate_pool>

<config>
playlist_size: 10
bridge_tracks: 3
discovery_tracks: 7
spotlight_artists: 4
radius_tier: "metro"
</config>

<output_schema>
{
  "taste_profile": {
    "headline": "string, max 8 words",
    "body": "string, 2–3 sentences, names ≥2 of their actual artists"
  },
  "spotlight_artists": [
    {
      "name": "string (must be from candidate_pool)",
      "one_liner": "string, 1 sentence on their sound",
      "why_this_matches_you": "string, 1–2 sentences, names ≥1 user top artist",
      "spotify_url": "string (from candidate_pool)"
    }
  ],
  "playlist": [
    {
      "track": "string",
      "artist": "string",
      "role": "bridge | discovery",
      "spotify_url": "string",
      "note": "string, optional, 1 line if role=discovery"
    }
  ]
}
</output_schema>
```

---

## 7. HTML output — the Spark Card

Single self-contained `.html` file. Inline CSS. System fonts + one Google Font fallback. No JS frameworks, no build step. Must render offline.

### Visual direction
- **Logo**: SparkFM wordmark in `/assets/sparkfm-logo.png` (Goyle to provide).
- **Palette**: purple/magenta/pink gradient from sparkfmonline.com's `sparkgradient.png`. Estimate: `#6B2FA8 → #C4428E → #F25A8F`. Verify against actual asset. Black + white for text surfaces.
- **Feel**: clean, modern urban-radio. Lots of negative space. Gradient as accent (banner strip, CTA button, thin border) — not a full wash.
- **Typography**: sans-serif system stack, Inter as webfont fallback.

### Structure (top to bottom)

1. **Header** — SparkFM logo left, "Spark Your Spotify" + date right.
2. **Hero** — "Hey {name}, here's your Spark" + `taste_profile.headline` as subheadline.
3. **Taste profile** — `taste_profile.body` as a pull quote with gradient left-border.
4. **Spotlight artists** — 3–5 cards with:
   - Artist name (bold) + small label "Based in {city}, {state}"
   - One-liner
   - "Why this matches you" bridge
   - "Listen on Spotify" button
   - Small pill showing current `radius_tier`: *Local to Greater Boston*
5. **Your Spark Playlist** — numbered list, 10 rows: `#. Track — Artist` with `[bridge]`/`[discovery]` tag. Discovery rows visually distinguished.
6. **Footer CTA** — Three buttons:
   - "Listen to SparkFM Live" → sparkfmonline.com/live
   - "Follow SparkFM" → instagram.com/sparkfmonline
   - "Donate to SparkFM" → givebutter.com/Sparktheculture
   Credit line: *"Inspired by SparkFM Online Radio • Boston, MA. Stations like Spark are how local scenes stay alive — find yours."*
7. **Quis line** — small italic below footer: *Quis claims he's not a bitch.*

### Mobile
Must look good on a 375px-wide phone screen. Single-column layout, max width ~480px.

---

## 8. Evaluation

Run against three test personas (fixtures in `tests/fixtures/`):

1. **R&B head in Boston** — SZA, Jazmine, Summer Walker, Kehlani dominant. Expected: Boston neo-soul / R&B locals surfaced.
2. **Hip-hop head in Atlanta** — Kendrick, Cole, JID, Future. Expected: ATL hip-hop discoveries (not the already-famous ones).
3. **Indie listener in rural Vermont** — Phoebe Bridgers, Big Thief, Sufjan. Expected: adaptive radius expands to New England; northern NE indie surfaced.

Manual review checks:
- [ ] Does the taste profile sound like a friend, not a chatbot?
- [ ] Does every "why this matches you" name a real artist from the top artists?
- [ ] Are there zero hallucinated artists (every rec traceable to candidate pool)?
- [ ] Does the radius label on the HTML match the tier the tool actually used?
- [ ] Does the VT persona get "Northern New England" or "New England" and not "Vermont" (which would yield nothing)?
- [ ] Does the HTML render cleanly on a 375px screenshot?
- [ ] Would SparkFM be proud to be credited on this?

The last one is the only one that actually matters.

---

## 9. Implementation notes

### Language
Python 3.11+ preferred. `spotipy` + `requests` + `jinja2` + `anthropic` SDK covers everything.

### Project structure
```
spark/
  src/
    spotify_client.py     # OAuth + API wrapper
    location.py           # IP geo + confirmation prompt
    candidates/
      spotify_graph.py    # related-artists + search
      musicbrainz.py      # geo enrichment with caching
      pool.py             # merge + adaptive radius + score
    claude_client.py
    renderer.py
    cli.py
  data/
    regions.json          # US regions lookup
  templates/
    spark_card.html.j2
  assets/
    sparkfm-logo.png
  tests/
    fixtures/             # 3 test personas as saved TasteSnapshots
  .env.example
  README.md
  SPEC.md
```

### Config / secrets — `.env`
- `ANTHROPIC_API_KEY`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REDIRECT_URI` (default `http://127.0.0.1:8765/callback`)

### Caching (important)
- MusicBrainz lookups: 30 day TTL, local JSON cache
- Spotify related-artists: 7 day TTL
- Honor MusicBrainz's 1 req/sec limit strictly

### Error handling
- If Claude returns non-JSON or fails schema validation: retry once with stricter prompt, then fail loudly.
- If adaptive radius exhausts all tiers and pool is still < 10: render anyway, note sparseness in the taste profile, tell the user their scene is small (not their fault).
- If user is outside the US: v1 error message: "Spark Your Spotify is US-only for now. International coming soon."

### Logging
Local `spark.log` — each run's snapshot, pool size, radius tier used, Claude response, render path. No telemetry.

---

## 10. Handoff checklist for Goyle

- [ ] High-res SparkFM logo PNG (transparent background)
- [ ] Confirm palette hex values from Spark's actual gradient asset
- [ ] Spotify developer app: client ID + secret (free, 5 min at developer.spotify.com)
- [ ] Confirm Quis line is approved for inclusion
- [ ] Confirm SparkFM has given informal thumbs-up to being credited as the inspiration (not strictly required since the credit is positive, but good manners)
