# ai-shim — Transport-Shim fuer wardrowbe -> Gemini

## Warum

Wardrowbe schickt an seinen OpenAI-kompatiblen Endpoint drei Felder, die
Googles Gemini-Endpoint ablehnt:

| Feld | Warum wardrowbe es sendet | Antwort von Gemini |
|---|---|---|
| `logprobs` + `top_logprobs` | erster Tagging-Pass berechnet daraus einen Konfidenzwert | `400 INVALID_ARGUMENT` |
| `reasoning_effort: "none"` | Standardwert in `app/config.py`; der Validator macht aus einem leeren Env-Wert wieder `"none"` | `400 INVALID_ARGUMENT` |

Wardrowbes eigene Rueckfall-Logik greift dafuer nicht, weil sie erst nach
`response.raise_for_status()` einsetzt -- ein 400 fliegt also direkt durch.

## Was der Shim macht

Nimmt jeden Request an, entfernt die drei Felder aus dem JSON-Body und leitet
sonst unveraendert an `https://generativelanguage.googleapis.com` weiter.
Keine Fachlogik, keine Kenntnis von Wardrowbe-Versionen -- deshalb bricht ein
Wardrowbe-Update den Shim nicht.

## Betrieb

```sh
cd /opt/docker-compose/ai-shim
docker compose up -d --build
docker logs wardrobe-shim --since 5m     # zeigt Request-Keys und Upstream-Fehler
```

Wardrowbe zeigt per `AI_BASE_URL=http://wardrobe-shim:8080/v1beta/openai`
hierher. Der Shim haengt ausschliesslich im internen Netz `wardrobe_internal`
und ist von aussen nicht erreichbar.

## Kostenloser Nebeneffekt

`logprobs` ist bei Gemini ohnehin nicht verfuegbar; der Konfidenzwert
(`logprobs_confidence`) bleibt damit `None`. Wardrowbe nutzt stattdessen seine
eigene Vollstaendigkeits-Heuristik (`compute_tag_completeness`).
