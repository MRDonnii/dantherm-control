# Fase 2: Driftssikkerhed (udgivelsesnoter, ikke udgivet endnu)

Ændringer af Claude AI.

## Rettelser

- **Master-styringen kunne gå i stykker.** Tre dele af programmet ændrede den samme liste på én gang. Det gav fejlen "deque mutated during iteration" i loggen. Listen er nu låst, så kun én del ad gangen kan ændre den.
- **Eftervarme og pejs blev ikke sat tilbage ved stop.** Når gatewayen stoppede, blev eftervarmens setpunkt ikke sat tilbage til det almindelige mål, så HAC1 kunne blive stående på et højt setpunkt. Pejsens blæserværdier blev heller ikke gendannet. Nu sker begge dele, hver gang tjenesten stopper. Det gælder også, når vagthunden genstarter den. Hvis HCP4-panelet er master, skriver Pi'en stadig ikke noget.
- **Advarsler og fejl kan nu ses i loggen.** Før blev alle linjer gemt som almindelig info i systemets log, så `journalctl -p warning` viste ingenting. Nu har hver linje sin rigtige alvorsgrad. Tjenesterne hedder `hch5-gateway` og `hch5-onewire` i loggen. Linjen "TCP-læser tilsluttet", der kom hvert minut, vises kun i fejlsøgningsloggen.
- **Vagthund på RS485-løkken.** Hvis læsning og skrivning på bussen går i stå i mere end 90 sekunder, genstarter gatewayen af sig selv. En skrivning på bussen kan heller ikke længere hænge for evigt.
- **Gamle styrekommandoer bliver ikke udført for sent.** En kommando, som styringen allerede har opgivet efter 4 sekunder, bliver nu sprunget over i stedet for at blive udført senere.
- **Blæsere og bypass skrives igen, hvis anlægget mister indstillingen.** Hvis anlægget i mere end 30 sekunder viser andre blæserværdier eller en anden bypass end dem, Pi'en har skrevet, skriver Pi'en dem igen. Det sker højst én gang hvert 10. minut. Anlæggets egen frostsikring bliver ikke forstyrret. Det samme gælder pejs og standby.
- **Tællere for RS485.** For hver enhed (anlægget og HAC1) tælles svar, gentagelser, svar der udebliver, og svar med fejl i kontrolsummen. Tallene ligger i tilstanden som `rs485_unit_*` og `rs485_hac1_*`.
- **Sniffer-hukommelsen vokser ikke længere uden grænse.**
- **Frostlåsens tider følger ikke længere uret.** De 2 minutter, før låsen slår til, og de 30 minutter, før den slipper, måles nu, så de ikke påvirkes, hvis uret bliver stillet efter opstart. Pi'en har intet batteriur. Tjenesten starter nu også først, når uret er synkroniseret.
- **Forkerte værdier i en indstilling giver nu en fejlbesked.** Før blev forbindelsen afbrudt uden svar. Indstillingerne bliver stadig ikke ændret.
- **Home Assistants nøgle kan ikke længere ændre sikkerhedsindstillinger.** Det gælder frostsikringen, følerroller, rumopsætningen og typen af varmeflade. En administratornøgle eller en bruger, der er logget ind, kan stadig ændre dem. Alt det, Home Assistant selv bruger, virker som før.
- **Login og WebUI:**
  - Når login er slået fra, kræves der nu stadig et sikkerhedstoken. Det beskytter mod falske forespørgsler fra andre hjemmesider. Siderne for ugeplan og indregulering virker nu også, når login er slået fra.
  - Hvis login-filen bliver beskadiget, åbner opsætningen ikke igen. Pi'en bruger i stedet den sidste gode kopi. Kan den heller ikke læses, forbliver login låst.
  - Sessioner er beskyttet, når flere bruger siden på én gang. En gammel cookie får ikke længere Pi'en til at skrive til SD-kortet ved hver forespørgsel.
  - Siden er bedre beskyttet mod at blive lagt ned fra hjemmenettet. En forbindelse, der ikke sender noget, lukkes efter 20 sekunder. En forkert længde i forespørgslen får ikke længere siden til at hænge. Grænsen på fem loginforsøg kan ikke omgås, ved at mange prøver på én gang. Højst to adgangskoder kontrolleres samtidig.
- **Optagelser stopper af sig selv.** Sniffer- og retningsoptagelser stopper efter 2 timer eller 200 MB, og kun de 5 nyeste gemmes. Du kan nu også starte en ny optagelse lige efter, at du har stoppet en.
- **Opdateringer:**
  - Går en opdatering i stå, bliver den nu stoppet på en måde, så den gamle version bliver sat tilbage. Før blev processen dræbt, og så skete det ikke.
  - To opdateringer kan ikke startes på samme tid.
  - Backuppen indeholder nu også styringens egne filer (controller.json, pejs, filter, nat m.m.), og de bliver lagt tilbage, hvis opdateringen fejler.
  - Den nye version bliver holdt øje med i 5 minutter, før den bliver godkendt.
  - Gamle WebUI-filer og opdateringsbackups bliver ryddet op. De 10 nyeste backups og dem fra de sidste 37 dage bliver gemt.
  - Hvis udgivelsen har en SHA256SUMS-fil, kontrolleres alle de hentede filer mod den.

## Ændret

- **Opdatering venter, mens det fryser.** Når eftervarmens frostsikring er slået til, starter en opdatering ikke af sig selv. WebUI'en spørger, om du vil opdatere alligevel. Hvis du kører `update.sh` i hånden, skal du sætte `HCH5_UPDATE_FORCE=1`, når det fryser.

## Nyt (slået fra som standard)

- **"Trin 6 slutter efter 4 timer"** (Indstillinger, Trinstyring, kun med 6 trin). Slå den til, hvis trin 6 skal gå ned på trin 5 af sig selv efter 4 timer, ligesom trin 4 gør med 4 trin.
