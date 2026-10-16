# ntfy - zentraler Notification-Hub

Self-hosted **ntfy v2.31.0** als Push-Transport fuers Home Lab.
Oeffentlich: https://ntfy.mueller-nas.de (Traefik + Cloudflare-Wildcard).

## Betrieb
- Compose: /opt/docker-compose/ntfy/docker-compose.yml
- Daten:   /etc/docker/ntfy (cache.db, auth.db, attachments)
- Netz:    proxy (Traefik, exposedByDefault=false)
- Auth:    `NTFY_AUTH_DEFAULT_ACCESS=deny-all` + eigene User/Tokens
- KEIN Authentik-forwardAuth: die nativen Clients (Android-App, Skripte, Gateway)
  authentifizieren sich per Token/Basic an der API; forwardAuth wuerde sie brechen.

## Topics (Scopes)
| Topic  | Inhalt                                                     | Client-Empfehlung             |
|--------|------------------------------------------------------------|-------------------------------|
| crit   | Wasserleck, Rauch, Tuerklingel, Einbruch, Backup-Fehler     | min_priority 1 (immer)        |
| ai     | OpenCode/Hermes: fertig, Frage, Fehler                      | min_priority 3                |
| infra  | Hestia: Konto-Wechsel, Rollout, Failover, Zertifikate, CI    | min_priority 3                |
| home   | HA-Alltag: Fenster, Regen, Lueften, Temperatur              | min_priority 2 (Digest folgt) |
| print  | 3D-Drucker fertig                                           | min_priority 3                |
| digest | Tages-/Abendzusammenfassung                                 | min_priority 2                |

## User / ACL
| User           | Rolle | Rechte                        |
|----------------|-------|-------------------------------|
| carsten        | admin | alles (Web-UI + Android-Login)|
| src-ha         | user  | write-only: crit, home, print |
| src-opencode   | user  | write-only: ai                |
| src-hestia     | user  | write-only: infra             |
| src-monitoring | user  | write-only: crit, infra       |

Ein User pro Quelle: ntfy-Tokens haben vollen Account-Zugriff, das Scoping passiert
deshalb ueber die Topic-ACL des jeweiligen Users.

## Zugangsdaten
`/etc/docker/ntfy/credentials.env` (0600, ausserhalb des Repos).
Migration nach OpenBao `secret/data/mcp/notify` steht noch aus (Admin-Token noetig).

## Publish (Beispiel)
```bash
TOKEN=$(grep NTFY_TOKEN_SRC_OPENCODE /etc/docker/ntfy/credentials.env | cut -d= -f2)
curl -H "Authorization: Bearer $TOKEN" \
     -H "Title: OpenCode" -H "Priority: 4" -H "Tags: robot" \
     -d "Session fertig: <TLDR>" https://ntfy.mueller-nas.de/ai
```

## Naechste Schritte
- Phase 2: Quellen einhaengen (Hestia _event(), Uptime Kuma, Grafana, HA rest_command)
- Phase 3: OpenCode-Plugin (session.idle / permission.asked), Hermes-Webhook
- Phase 5: notify-Gateway (Digest, Ruhezeiten, Antworten A/B/C)
