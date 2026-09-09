# Nexus Memory - Brand Style Guide

> Version 1.0 | 20. Juni 2026
> Eine Living Document. Mit jedem Design-Update erweitern.

---

## 1. Logo-Dateien

| Datei | Format | Verwendung |
|-------|--------|-----------|
| `nexus-logo-vertical.png` | 544x619 | Social Media, GitHub Profile, App Icon |
| `nexus-logo-horizontal-slogan.png` | 614x220 | README Header, Website Hero, Banner |
| `nexus-logo-horizontal-no-slogan.png` | 602x210 | Navigation Bar, Footer, Visitenkarte |
| `nexus-logo-square.png` | 325x385 | Avatar, Favicon, Thumbnail |
| `nexus-icon.png` | 325x265 | App Icon, Watermark, Favicon |

---

## 2. Farbpalette

### Primärfarben

| Farbe | HEX | RGB | CMYK | Pantone | Verwendung |
|-------|-----|-----|------|---------|------------|
| **Purple** | `#954EFF` | 149, 78, 255 | 42, 69, 0, 0 | Pantone 2725 C | Linke Gehirnhälfte, "Use" im Slogan |
| **Cyan** | `#02C1FF` | 2, 193, 255 | 99, 24, 0, 0 | Pantone 319 C | Rechte Gehirnhälfte, "Remember" im Slogan |
| **White** | `#DEDDDE` | 222, 221, 222 | 0, 0, 0, 13 | Pantone 663 C | NEXUS Text |

### Sekundärfarben

| Farbe | HEX | RGB | Verwendung |
|-------|-----|-----|------------|
| **Gradient Purple** | `#9769EC` | 151, 105, 236 | Durchschnittswert Purple |
| **Gradient Cyan** | `#119BE9` | 17, 155, 233 | Durchschnittswert Cyan |

### Hintergrundfarben

| Farbe | HEX | RGB | Verwendung |
|-------|-----|-----|------------|
| **Dark Background** | `#050810` | 5, 8, 16 | Logo-Hintergrund, Dark Mode |
| **Pure Black** | `#000000` | 0, 0, 0 | Aussenbereich, Rahmen |

### MEMORY-Text Gradient

Der Gradient verläuft links nach rechts von Cyan zu Purple:

```
Cyan (#02C1FF) → Purple (#954EFF)
```

### Trennlinie Gradient (Horizontal mit Slogan)

Der Gradient verläuft von oben nach unten:

```
Purple (#954EFF) [oben] → Cyan (#02C1FF) [unten]
```

---

## 3. Typografie

### Hausschrift

Die ChatGPT-generierte Schriftart ist eine **geometrische Sans-Serif** im Stil von:

- **Google Inter Bold** (Web-Ersatz, kostenlos via Google Fonts)
- **Alternative:** SF Pro Display Bold (macOS system font)
- **Open Source Alternative:** Manrope Bold

| Einsatz | Gewicht | Größe relativ |
|---------|---------|---------------|
| **NEXUS** | Bold (700) | 100% (Referenz) |
| **MEMORY** | Bold (700) | ~80% von NEXUS |
| **Tagline 1** ("The Memory Hub...") | Regular (400) | ~40% von NEXUS |
| **Tagline 2** ("Remember once...") | Regular (400) | ~40% von NEXUS |

### Buchstabenabstand

- NEXUS: Normal (0em)
- MEMORY: Leicht gesperrt (letter-spacing: 0.02em)
- Taglines: Normal (0em)

---

## 4. Logo-Nutzung

### Mindestgröessen

| Variante | Mindestbreite | Mindesthoehe |
|----------|--------------|--------------|
| Horizontal | 300px | 100px |
| Vertical | 200px | 230px |
| Square | 120px | 140px |
| Icon only | 48px | 40px |

### Schutzraum (Clear Space)

Der freie Abstand um das Logo muss mindestens **25% der Logo-Höhe** betragen.

```
┌─────────────────────────────┐
│                             │
│   ┌─────────────────┐       │
│   │   NEXUS MEMORY  │       │
│   └─────────────────┘       │
│                             │
└─────────────────────────────┘
  ←───── Schutzraum ──────→
```

### Erlaubte Hintergruende

| Hintergrund | erlaubt? |
|------------|----------|
| `#050810` (Dark Background) | ✅ Optimal |
| `#000000` (Pure Black) | ✅ Gut |
| `#1a1a2e` (Dark Navy) | ✅ Akzeptabel |
| Weiss (`#FFFFFF`) | ❌ Nicht erlaubt |
| Grau < `#333333` | ❌ Nicht erlaubt |
| Bunte Hintergruende | ❌ Nicht erlaubt |

### Verbotene Nutzungen

- ❌ Logo stauchen, strecken oder verzerren
- ❌ Farben aendern oder austauschen
- ❌ Schatten oder Glow-Effekte hinzufuegen (ist im Logo bereits enthalten)
- ❌ Logo drehen oder kippen
- ❌ Logo auf hellem Hintergrund verwenden
- ❌ Logo-Elemente einzeln ohne Genehmigung verwenden
- ❌ Slogan ohne Logo verwenden

---

## 5. Logo-Varianten

### 5.1 Vertical (vertikal)

```
   ┌─────────────┐
   │   [GEHIRN]  │
   │             │
   │   NEXUS     │
   │   MEMORY    │
   │ ─────────── │
   │ The Memory  │
   │ Hub for AI  │
   │ Agents.     │
   │ Remember    │
   │ once. Use   │
   │ forever.    │
   └─────────────┘
```

Verwendung: Social Media Profile, GitHub Org Page, App Store

### 5.2 Horizontal mit Slogan

```
┌──────────────────────────────────────┐
│ [GEHIRN] │ NEXUS                     │
│          │ MEMORY                    │
│          │ ──────────                │
│          │ The Memory Hub for AI...  │
│          │ Remember once. Use forever│
└──────────────────────────────────────┘
```

Verwendung: README Header, Website Hero, Discord Banner

### 5.3 Horizontal ohne Slogan

```
┌──────────────────────────┐
│ [GEHIRN]   NEXUS          │
│            MEMORY         │
└──────────────────────────┘
```

Verwendung: Navigation Bar, Footer, Visitenkarte

### 5.4 Square (quadratisch)

```
   ┌─────────┐
   │[GEHIRN] │
   │         │
   │ NEXUS   │
   │ MEMORY  │
   └─────────┘
```

Verwendung: Avatar, Thumbnail, App Icon

### 5.5 Icon (nur Gehirn)

```
   ┌─────┐
   │BRAIN│
   └─────┘
```

Verwendung: Favicon, Watermark, Loading Spinner

---

## 6. Das Gehirn-Icon

### Design-Konzept

Das Gehirn-Icon ist vertikal gespalten und erzählt eine Geschichte:

| Haelfte | Farbe | Stil | Bedeutung |
|---------|-------|------|-----------|
| **Links** | Purple (#954EFF) | Organisch, dicht, geschlossen | Menschliches Gedaechtnis - komplex, verwinkelt |
| **Rechts** | Cyan (#02C1FF) | Geometrisch, offen, mit herausragenden Nodes | Digitale Verbindung - clean, strukturiert, offen fuer die Zukunft |

### Die herausragenden Nodes

Die rechte Gehirnhaelfte hat 4-8 Circuit-Nodes die ueber die aeussere Kontur hinausragen. Diese sind:

- Unterschiedlich lang (nicht symmetrisch)
- Symbolisieren Verbindungspunkte nach aussen (APIs, Agents, Systeme)
- Brechen die symmetrische Aussenform
- Verleihen dem Icon Dynamik und "Offenheit"

**Diese Nodes duerfen NICHT entfernt, verschoben oder symmetrisiert werden.**

---

## 7. Slogan-Verwendung

### Tagline 1: "The Memory Hub for AI Agents."

- Positionierung: Untertitel unter MEMORY
- Farbe: Weiss (#DEDDDE)
- Schriftgroesse: ~40% von NEXUS

### Tagline 2: "Remember once. Use forever."

- "Remember" = Cyan (#02C1FF)
- "once." = Weiss (#DEDDDE)
- "Use" = Purple (#954EFF)
- "forever." = Weiss (#DEDDDE)

**Farbzuordnung niemals aendern.**

---

## 8. Dateiformate

| Format | Verwendung | Verfügbarkeit |
|--------|------------|---------------|
| **PNG** | Web, README, Social Media | ✅ Alle 5 Varianten |
| **SVG** | Skalierbare Anwendungen | Geplant (mit eingebettetem PNG) |
| **ICO** | Favicon | Aus Icon ableitbar |
| **PDF** | Print, Visitenkarten | Geplant |

---

## 9. Quellen und Credits

- **Design:** Nebo (ChatGPT-generiert)
- **Vektorisierung/Anpassung:** Kiosha (Hermes Agent)
- **Quelle:** Desktop-SVG (NexusMemory_SVG-Vertikal.svg)
- **Lizenz:** Proprietär - Nexus Memory Projekt

---

*Dieses Brandbook ist verbindlich für alle Verwendung des Nexus Memory Logos.*