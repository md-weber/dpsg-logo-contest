# Logo-Wettbewerb — DPSG Bezirk Karlsruhe

Web-Plattform, mit der ein Pfadfinder-Bezirk ein **neues Logo** findet:
Mitglieder reichen Bild-Ideen ein und stimmen anschließend anonym mit einem
**Wahl-Token** ab. Gewonnen hat der Vorschlag mit den meisten Stimmen.

Läuft komplett auf **Python-Standardbibliothek** — kein Flask, kein pip, kein venv.
Damit ist es auf Hosts installierbar, auf denen man keine Pakete nachinstallieren kann.

---

## Inhalt

- [Funktionen](#funktionen)
- [Schnellstart (lokal)](#schnellstart-lokal)
- [Ablauf für Admins](#ablauf-für-admins)
- [Betrieb & Deployment](#betrieb--deployment)
- [Datenschutz](#datenschutz)
- [Tests](#tests)
- [Technische Hinweise](#technische-hinweise)

---

## Funktionen

**Für Mitglieder**
- Vorschlag einreichen: Name (öffentlich), optional Titel, optional E-Mail (nur fürs Orga-Team),
  Bild als **JPG, PNG oder SVG** (max. 8 MB, Magic-Bytes-geprüft)
- Abstimmen mit einem **einmaligen Wahl-Token** (z. B. `Wolf-Zelt-7B3F`)
- Eingabe tolerant: Groß-/Kleinschreibung, Leerzeichen und Bindestriche sind egal
- Kein Login, keine Konten, keine Tracking-Cookies
- Eigene Vorschläge sind auswählbar markiert; für das eigene Bild kann man nicht stimmen

**Für Admins** (Passwortgeschützt unter `/admin`)
- **Tokens pro Stamm/Gruppe** erzeugen (bis 500 auf einmal), jeweils mit fortlaufender Nummer
- **Druckfertige PDF-Bögen**: 12, 18 oder 24 Tokens pro A4-Seite, mit Stamm, Nummer,
  Abstimm-URL, Schnittlinien und Kompassrose — enthält nur *ungennutzte* Tokens
- **Phasen getrennt steuern**: Einreichen öffnen/schließen, Abstimmung öffnen/schließen,
  Ergebnis veröffentlichen (Zwischenstand bleibt bis dahin verborgen)
- **Moderation**: Vorschläge löschen (inkl. Bild und zugehöriger Stimmen)
- **Ergebnis**: Rangliste unter `/ergebnis` (für Admins immer sichtbar) + **CSV-Export**
- **Ungenutzte Tokens eines Stamms** invalidieren (z. B. wenn ein Bogen verloren geht)
- **Passwort ändern**, Runde zurücksetzen, **alles löschen** (DSGVO)

**Öffentlich**
- `/` — Galerie, Einreichen, Abstimmen
- `/ergebnis` — Ergebnis (erst nach Freigabe)
- `/hilfe` — Kurzinfo, Datenschutz und Admin-Anleitung (zum Teilen in der Gruppe)

---

## Schnellstart (lokal)

```bash
python3 app.py
# -> HTTP auf 127.0.0.1:8391
```

Beim ersten Start wird ein Admin-Passwort erzeugt und in
`admin-initial-password.txt` abgelegt (0600). **Nach dem ersten Login ändern**
(im Admin-Bereich, ganz unten) — die Datei verschwindet dann.

### Umgebungsvariablen

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8391` | Klartext-HTTP-Port |
| `HTTP_BIND` | `127.0.0.1` | Bind-Adresse für HTTP (öffentlich nur, wenn `0.0.0.0`) |
| `HTTPS_PORT` | `8443` | TLS-Port (wird nur gebunden, wenn ein Zertifikat vorliegt) |
| `TLS_CERT` / `TLS_KEY` | `tls/cert.pem`, `tls/key.pem` | Zertifikat + Key |
| `PUBLIC_URL` | — | Öffentliche Basis-URL (landet in den Token-PDFs) |
| `LOGO_DATA_DIR` | Ordner der App | Ablage für DB und Uploads |

---

## Ablauf für Admins

1. **Anmelden** unter `/admin`, Start-Passwort ändern.
2. **Tokens erzeugen**: Stammname + Anzahl Mitglieder eintragen. Pro Stamm entsteht
   eine eigene Liste mit stabiler Nummerierung.
3. **PDF drucken**: beim Stamm „PDF 12 / 18 / 24" wählen (Tokens pro A4-Seite),
   ausschneiden und verteilen. Das PDF enthält nur noch ungenutzte Tokens; die
   Nummern bleiben beim Nachdruck gleich.
4. **Bogen verloren?** „Ungenutzte löschen" beim Stamm macht alle noch nicht
   benutzten Tokens dieses Stamms ungültig — danach neue erzeugen.
5. **Phasen steuern**: Einreichen schließen, wenn genug Vorschläge da sind;
   danach die Abstimmung öffnen. Der Zwischenstand bleibt verborgen, bis
   „Ergebnis veröffentlichen" aktiviert wird.
6. **Moderieren**: unpassende Vorschläge löschen (am besten vor Beginn der Abstimmung).
7. **Auswerten**: `/ergebnis` für die Rangliste, CSV-Export für die Doku.
8. **Abschließen**: nach der Bekanntgabe „Alles löschen" — damit ist das
   Löschversprechen aus `/hilfe` erfüllt.

---

## Betrieb & Deployment

Läuft als systemd-**User**-Service (kein root nötig):

```bash
systemctl --user start|stop|restart|status logo-contest.service
journalctl --user -u logo-contest -n 100
```

### HTTPS

Der Dienst kann TLS direkt ausliefern (`HTTPS_PORT`), sobald ein Zertifikat
unter `tls/` liegt. Auf Hosts **ohne root** sind Ports < 1024 nicht bindbar —
dann z. B. **8443** verwenden.

Zertifikat per **DNS-01** (braucht keinen offenen Port, nur einen DNS-Eintrag):

```bash
HETZNER_TOKEN=<token> acme.sh --issue --dns dns_hetznercloud \
  -d beispiel.example.org --keylength ec-256
cp <acme>/fullchain.cer tls/cert.pem
cp <acme>/beispiel.example.org.key tls/key.pem && chmod 600 tls/key.pem
systemctl --user restart logo-contest.service
```

> **DNS-01** ist hier die richtige Wahl: Die Validierung läuft über einen
> TXT-Record, es muss also kein Port von außen erreichbar sein.
>
> **Achtung IPv6:** Zeigt ein `AAAA`-Record auf einen anderen Host als den
> eigenen, laufen IPv6-bevorzugende Clients in einen Timeout, obwohl IPv4
> funktioniert. Entweder `AAAA` entfernen (Fallback auf IPv4) oder korrekt setzen.

### Backup

Zustand sind nur die Datenbank und die Bilder: `contest.db` + `static/uploads/`.
Der private Schlüssel (`tls/`) und die DB sind per `.gitignore` vom Repo ausgeschlossen.

---

## Datenschutz

Ausgerichtet auf **Datenminimierung** (Art. 5 DSGVO):

| | |
|---|---|
| **Beim Einreichen** | Name (öffentlich), optional Titel, optional E-Mail (nur Orga-Team), Bild |
| **Beim Abstimmen** | Token wird als „verbraucht" markiert; die Stimme wird ohne Bezug zum Token gezählt (nur Tagesdatum) |
| **Nicht gespeichert** | keine Konten, keine Passwörter der Mitglieder, keine Tracking-Cookies, keine IP-Adressen, keine Zugriffsprotokolle |

- **Anonymität der Stimme:** Es wird *nicht* gespeichert, welches Token für welchen
  Vorschlag gestimmt hat — auch das Orga-Team kann das nicht nachvollziehen.
- **Missbrauchsschutz** ohne Protokollierung: Fehlversuche werden höchstens
  15 Minuten im Arbeitsspeicher gezählt (Rate-Limit für Login und Token-Raten).
- **Löschung:** „Alles löschen" im Admin-Bereich entfernt Vorschläge, Bilder,
  Stimmen und Tokens.

---

## Tests

```bash
python3 test_app.py
```

Startet eine echte Instanz in einem temporären Datenverzeichnis und prüft
u. a.: kein Cookie-Leak an anonyme Besucher, Path-Traversal blockiert,
CSRF-Schutz, Formulare wie im Browser (urlencoded), Magic-Bytes- und
SVG-Script-Prüfung, Phasen-Sperren, Ein-Stimme-pro-Token unter 12 parallelen
Anfragen, PDF-Struktur und -Layout, TLS inkl. fehlerhafter Handshakes,
sowie die DB-Migration aus älteren Schema-Versionen.

---

## Technische Hinweise

- **Nur Standardbibliothek** (`http.server` + `sqlite3`), ein Prozess.
- **PDF** wird in `pfadi_pdf.py` selbst erzeugt (Base-14-Fonts, zlib-Streams);
  Textbreiten werden mit den echten AFM-Metriken berechnet und automatisch in
  die Coupons eingepasst.
- **Uploads** werden anhand ihrer Magic Bytes geprüft, unter zufälligem Namen
  gespeichert und mit `Content-Security-Policy: sandbox` ausgeliefert
  (SVG mit Skripten wird abgelehnt).
- **Token** bestehen aus zwei Pfadfinder-Wörtern plus vier Hex-Zeichen
  (z. B. `Wolf-Zelt-7B3F`) — gut vorlesbar, aber nicht erratbar.
- **Admin-Auth** über PBKDF2-Passwort und serverseitige Session, Admin-Aktionen
  mit CSRF-Token.

---

## Lizenz

Internes Projekt des DPSG-Bezirks Karlsruhe.
