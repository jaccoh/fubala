# Fubala gebruiken in een project — quickstart voor een nieuwe AI-sessie

Fubala draait op **http://localhost:8000** (UI + API; repo:
de root van deze checkout). Starten:
`cd backend && uv run python -m app`. `FUBALA_HOST`/`FUBALA_PORT` in
`backend/.env` bepalen de bind (ook voor self-update-herstarts).

## Het model in één alinea

Fubala is een kanban-bord waar AI-stories door een pijplijn gaan:
spec → AI-review → **spec-gate (mens)** → coder (geïsoleerde git-worktree)
→ verify (test-suite in de worktree, met fix-rondes) → AI-code-review →
**code-gate (mens)** → merge naar main. Jij bent de gates; de rest loopt
vanzelf. Twee assen per story: **executor** (op het project: intern of
external-claim) en **kind** (code | info | prep — het kind bepaalt wat de
gate beoordeelt).

## Jouw project erop zetten

1. **Project aanmaken**: UI → Projects → Browse… naar je repo-map. Zet
   `executor` op `internal` (fubala's eigen coder werkt in een worktree van
   jouw repo) — `external` is voor sessies die zelf willen bouwen en via
   claim/complete werken.
2. **Verify-command per project** (optioneel maar aan te raden): het commando
   dat na het coderen in de worktree draait, bijv.
   `cd backend && uv run pytest -q -n auto --maxprocesses=4`. Gefaalde verify
   blokkeert de merge.
3. **Context-bestanden**: `AGENTS.md` / `HANDOVER.md` in de repo-root worden
   automatisch in de spec/coder-prompts geïnjecteerd — zet hier je
   afspraken, testcommando's en conventions.
4. **Delivery-modus** (optioneel): `merge` (default) merged de story-branch
   in de root-checkout van het project. `push` duwt de branch
   `fubala/<story-id>` naar de `origin`-remote en raakt de checkout nooit
   aan — handig als de main branch beschermd is of als jij zelf in de map
   werkt. Vul daarnaast alle vier de PR-velden in (API-base, `owner/name`
   repo, base-branch en een pad naar een token-bestand) en goedkeuren opent
   meteen een Gitea-pull-request. Zet je één veld leeg, dan is het alleen
   pushen. Let op: in `push`-modus start de server zichzelf niet opnieuw op
   (self-update staat uit), en de token wordt alleen op het moment van
   goedkeuren uit het bestand gelezen — nooit opgeslagen of gelogd.

## Stories aanmaken (de belangrijke regels)

- **Klein en opsombaar**: een story met concrete AC's haalt de gate in één
  keer; "sweep"/"maak het beter"-stories verlammen de coder. Groot werk =
  prep-story die kinderen voorstelt, of zelf opsplitsen.
- **Modelkeuze**: `zai/glm-5.3` voor volwaardig werk;
  `zai/glm-5.3-flash` voor kleine begrensde wijzigingen (goedkoop, snel,
  bewezen capabel — niet voor grote architectuur).
- **Kind meenemen**: `info` voor documenten/onderzoek (geen worktree,
  verify = n.v.t.), `prep` voor opsplitsvoorstellen, `code` (default) voor
  het gewone werk.

## Werken via de API/MCP (voor agent-sessies)

MCP-server: `uv --directory backend run python -m app.mcp`. Tools:
`create_story`, `list_stories`, `get_story`, `story_action`
(queue/approve/reject/release), `add_story_note`, `wait_for_story`
(blokkeert met live progress-notificaties). Of direct REST:
`POST /api/v1/stories/{id}/queue|approve|reject|release|notes|evidence`.
**Operator-extra's**: `approve {override_verify, justification}` voor
werktree-artefacten, `retry {stage}`, `re-spec` — DB-chirurgie is nooit
nodig.

## Goed om te weten

- `spec phase produced no spec` betekent: het model heeft zijn output-budget
  verbrand aan denken (thinking) in plaats van de spec te schrijven. Fubala
  probeert het één keer opnieuw met een groter budget en een correctieve
  nudge; blijft dat zonder resultaat, dan faalt de story in plaats van dat
  stille narratie als spec wordt goedgekeurd. **Remedy:** verhoog
  `spec_max_output_tokens` in Settings → Application (leeg = de gateway-default `FUBALA_MAX_OUTPUT_TOKENS`, 16384) en
  retry de story; verlaag `agent_max_output_tokens_cap` (default 32768)
  als je het plafond voor cap-hit-escalaties wilt begrenzen.
- Provider-storingen (z.ai MidStream/timeouts én interne netwerkfouten /
  5xx) requeuen zichzelf met backoff, begrensd op
  `max_provider_retries` (default 5) pogingen — daarna `failed` met de
  laatste fout. Rate limits (quota, z.ai 1302) blijven onbegrensd geduldig. Afhankelijke stories wachten tijdens de retries en cascaden
  pas mee bij permanente fout of cancel. Verder: gehangen runs ruimt de
  watchdog; stille external-claims vervallen na 2 uur (lease). `failed`
  betekent: menselijk oordeel nodig.
- Elke merge herstart de server automatisch op de nieuwe code
  (self-update, met auto-revert als die niet boot).
- De menubalk-app (macos/dist/Fubala.app) notificeert OS-breed zodra een
  story bij jouw gate ligt.
- Reviewen doe je met evidence: lees de verify-uitkomst en de diff (of
  evidence bij external), niet het verdict. Afkeuren = concrete reden
  geven; de volgende coder-ronde krijgt die als opdracht.
- Wordt een spec na elkaar door de AI-review afgekeurd, dan parkeert de
  story bij jouw gate in plaats van eeuwig door te loopen: na
  `spec_reject_limit` (standaard 2) opeenvolgende afwijzingen. Dan kies
  jij: goedkeuren, afkeuren met nieuwe aanwijzingen, of annuleren. De
  teller reset bij elke goedkeuring (AI of mens).
