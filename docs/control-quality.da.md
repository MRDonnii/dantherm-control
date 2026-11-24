# Målingernes alder og prøvekørsel

Fra den offentlige Pi-beta 1.13.0-beta.1 (samme funktioner som den interne RC3) og HA-integrationen 0.9.0-beta.13 følger alderen på den seneste rapport fra sensoren med hver temperatur-, fugt-, CO₂- og PM2.5-værdi. En ny videresendelse fra HA gør ikke en gammel måling frisk. Alderen tælles videre med Pi'ens monotone ur mellem beskederne.

En enkelt måling udløber efter tre timer. Det er en forsigtig grænse, som også passer til batterisensorer, der kun rapporterer ved ændringer, og følger den eksisterende temperaturgrænse. Den enkelte værdi udelades fra reguleringen; rummets øvrige friske værdier kan stadig bruges. HA-forbindelsens eksisterende korte lease gælder samtidig. Uden HA falder Smart Auto tilbage til Local Auto.

På **Rum** vises for gamle målinger og ukendt alder. En ældre HA-klient uden aldersmetadata fungerer fortsat og mærkes med ukendt alder. Den nye klient markerer sin protokolversion; manglende eller ugyldig alder i dens beskeder betyder, at den pågældende måling udelades.

## Prøvekør styringen

**Diagnostik → Prøvekør styringen** sammenligner de nuværende indstillinger med et forslag. Knappen gemmer ingen indstillinger og sender ingen kommando til anlægget.

- **Local Auto** bruger de gemte lokale CO₂- og fugtgennemsnit. Ældre historik uden fugt kan kun sammenligne CO₂.
- **Smart Auto-behov** bruger optagne rumværdier, deres alder, lokale målinger og relevante eksterne signaler. Den genbruger beregningen af prioritet, CO₂, fugt, PM2.5, badeværelsesudtørring og holdetider. Rumprioriteter i formularen gælder kun for forslaget.

Smart Auto-optagelser starter efter denne opdatering, én gang i minuttet. De gemmes lokalt i `controller-inputs.sqlite3` ved siden af den almindelige historik: højst 4.320 optagelser og tre døgn. Prøven kan højst hente 24 timer. Filen indeholder rumnavne og sensormålinger; almindelige API-adgangsnøgler og passwords optages ikke. Optagelse og databasearbejde kører på dashboardets historiktråd, aldrig på regulatorens eller serieportens tråd. Ved databasefejl fortsætter reguleringen.

Prøven sammenligner **behovet før ugeplan, nat, vinter- og tørluftbegrænsning**. Eftervarmen og husets fysiske temperatur-/fugtrespons simuleres ikke. Den anvender dagens indstillinger på periodens målinger; tidligere indstillingsændringer genskabes ikke. Begyndelsen af perioden og datahuller over tre minutter nulstiller Smart Auto-holdetider og fugthistorik. Korte udsving mellem optagelser kan mangle. Brug resultatet som sammenligningsgrundlag, ikke som en forudsigelse af det faktiske ventilatortrin eller temperaturen.

## Eftervarmeanalyse

Historiksiden kan afgrænse analysen til normal drift, frostbeskyttelse, opstart, boost, køling, nyligt ventilatorskift, midlertidigt forsøg eller ekstern master. Første fem minutter er opstart; ændrede ventilatorprocenter markeres i tre minutter. Ved flere samtidige tilstande prioriteres ekstern master, slukket, frost, opstart, forsøg, køling, boost og ventilatorskift i den rækkefølge.

Gamle datapunkter og gennemsnitsperioder med flere tilstande vises som ukendte/blandede. Analysen forbinder ikke intervaller hen over udeladte tilstande eller datahuller. Tallene er stadig observationer på historiske gennemsnit.

Dette tilføjer alene en analysemarkør. Den eksisterende indirekte HAC1-eftervarmestyring, dens setpunkter, timing og frostbeskyttelse bevares. Direkte procentvis 0–10 V-styring kræver det kommende hardwarekort og indgår ikke her.
