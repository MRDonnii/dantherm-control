# Fase 6: Udbygninger

Fire små udbygninger. De tre nye funktioner er slået fra, til du slår dem til under Indstillinger. Ændret af Claude AI.

## Hvad er nyt

- **Ferie: koldere indblæsning** (Indstillinger → Eftervarme). Mens ferie kører, varmes luften mindre op. Hvor meget koldere vælger du selv, standard er 2 °C. Bruges Mål fra rummene, sænkes rummenes mål i stedet, så luften bliver koldere lidt ad gangen. Selve eftervarmestyringen er ikke ændret, den får bare et lavere mål. Frostsikringen virker som altid. Når ferien slutter, går målet tilbage. Med Mål fra rummene sker det lidt ad gangen.
- **Ingen eftervarme under frikøling** (Indstillinger → Frikøling). Når bypass er åben for at køle huset, sendes 10 °C til HAC1, ligesom ved Køl og natkøling. Så varmes den kølige luft ikke op igen. Frostsikringen vinder stadig.
- **Tør luft: skru op lidt ad gangen** (Indstillinger → Beskyttelse mod tør luft). Før sprang anlægget fra trin 2 helt op til CO₂-trinnet (fx trin 5), så snart CO₂ kom over grænsen. Nu går det til trin 3, mens rummene stadig er tørre, og så et trin mere for hver 200 ppm CO₂ over grænsen. Du kan vælge et andet antal ppm. Badeværelser og fugtige rum, der skal tørres ud, får stadig fuld fart med det samme.
- **Vekslerfrost gemmes i historikken.** HCH5 skruer selv ned for indblæsningen, når veksleren kan fryse til. Det bliver nu gemt hvert minut i en ny kolonne `unit_frost_protection` (1 = anlægget beskytter sig mod frost). Indblæsningens procent findes allerede i `fan_supply_percent`.

## Vekslerfrost: hvorfor Pi'en ikke selv gør noget endnu

De sidste 30 dage var afkastluften (T4) under 3 °C i ca. 96 timer og under 2 °C i ca. 33 timer. Den laveste måling var 0,9 °C, og T1 var aldrig under −2 °C. Forholdet mellem indblæsning og udsugning var det samme ved kold T4 som ellers, så HCH5's egen frostsikring har ikke været i gang. Veksleren fryser først til, når afkastluften kommer under 0 °C.

Hvis Pi'en skulle skrue ned ved T4 under 3 °C, som Nilan gør, ville indblæsningen være skruet ned i ca. 96 timer i en mild november uden grund. Huset ville få undertryk og mere kold luft gennem utætheder. HCH5 klarer det allerede selv, så Pi'en gør ikke noget endnu. Når den nye kolonne har vist, hvor tit og hvor længe HCH5 skruer ned i rigtig frost, kan vi tage stilling igen.

## Nye indstillinger

| Nøgle | Standard | Betydning |
|---|---|---|
| `vacation_afterheat_enabled` | fra | Ferie sænker eftervarmens mål |
| `vacation_afterheat_drop_k` | 2,0 (0,5–8) | Så mange grader koldere under ferie |
| `cooling_afterheat_hold_enabled` | fra | Ingen eftervarme under frikøling |
| `dry_release_steps_enabled` | fra | Tør luft slipper ét trin ad gangen |
| `dry_release_step_ppm` | 200 (50–500) | CO₂ over grænsen for hvert trin mere |

## Hvad du skal se efter

- Ferie med funktionen slået til: Eftervarmens mål er 2 °C lavere, og årsagen siger "ferie: 2 K koldere".
- Frikøling: Når bypass er åben, viser eftervarmen "Frikøling: eftervarme holdt nede", og T2 efter eftervarmen er næsten den samme som før eftervarmen.
- Tør luft: Når CO₂ kommer over grænsen, viser årsagen "tør luft slipper i trin: højst trin 3 lige nu". Trinnet går ikke fra 2 til 5 i ét spring.
- Historik: Kolonnen `unit_frost_protection` er 0 i mildt vejr. På frostdage viser den, hvornår HCH5 skruer ned for indblæsningen.
