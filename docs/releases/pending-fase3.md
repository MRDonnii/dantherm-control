# Fase 3: Alarmer og data, der kan stoles på (til 1.13.0-beta.14)

Ændringer af Claude AI. Eftervarmens styring, dens algoritmer og indstillinger er ikke rørt. Alarmerne vises i WebUI'ens begivenheder og som badge, og kommer med i den eksisterende mail. Der sendes ingen push til telefonen.

## T2-føleren viser for lidt

- Den målte T2 ligger cirka 1,5 K under luften før fladen. Det samme tal fås på alle eftervarmetrin (1148 minutter), og anlæggets egen føler efter fladen er enig, når fladen er kold (0,07 K afvigelse over 17 dage uden vand).
- "Korrektion T2" sættes derfor én gang fra 0,4 til 1,5, hvis den stod på 0,4. Står den på en anden værdi, er det dit eget valg, og den bliver. Rettelsen bruges kun til løft, kWh og diagnostik, aldrig i selve eftervarmestyringen.
- Historikkens "Eftervarme-skala" bruger nu samme korrektion, så løftet pr. trin ikke længere er cirka 1,5 K for højt.
- Hvis fladen er kold længe nok (trin 0, intet varmt vand), regner Pi'en selv et forslag til korrektionen ud. Forslaget ligger i diagnostikken, og intet ændres af sig selv.
- Alarmen "ingen varme" kan nu også udløses ved et højt trin (6 eller mere) i 20 minutter uden løft. Ny alarm "koldt vand i fladen": høj efterspørgsel, men flowet er koldt eller returen langt under luften (15 minutter). På de 30 dages data udløses den kun den ene gang, det rent faktisk skete (21. november).

## Nye alarmer

- Vandfladen nær frost (frostføler under 10 °C i 2 minutter). Laveste værdi på 30 dage var 15,2 °C.
- En CO₂- eller fugtmåler i et rum med styring, der mangler eller ikke har sendt i lang tid (navngiver rummet).
- Eftervarmen er faldet tilbage til anlæggets egen styring 3 gange eller mere inden for 6 timer. Hver tilbagefald skrives også i ændringsloggen.
- Eftervarmen skifter trin meget ofte (info). Før stod det kun i loggen på Pi'en.
- Ventilatorens omdrejninger passer ikke til trinnet (mere end 8 % i 10 minutter). Pi'en lærer selv, hvad hvert trin plejer at give. Indblæsningen springes over, mens anlæggets egen frostsikring skærer ned.
- Anlægget følger ikke det beordrede trin (mere end 5 point forskel i 10 minutter, når Pi'en styrer).
- En måler står stille på samme værdi i 4 timer eller mere (info). Længste stille periode i de 30 dage var under 1 time.
- Pi'en: disken 90 % fuld, for lav spænding, optagelsen af historik sat på pause i 30 minutter, uret ikke synkroniseret i 30 minutter (info).
- Eftervarmens status opdateres ikke (info).
- Varmegenvindingen er faldet 4 point eller mere over den seneste uge i forhold til de seneste 30 dage (info).

## Alarmkvalitet

- En alarm rydder først, når årsagen har været væk i mindst 5 minutter (halvdelen af alarmens forsinkelse, hvis den er længere). Den svinger dermed ikke frem og tilbage.
- Mail: en alarm, der kommer tilbage, mailes ikke som ny inden for en time efter den sidste mail. "Løst"-mailen sendes først, når alarmen har været væk i 15 minutter. Kommer den tilbage før, sker der ingenting.
- Filtertjekket får en reference for hvert trin, der mangler en, også længe efter et filterskift. Diagnostikken fortæller, om referencen stammer fra rene filtre eller er lært senere. Tidligere kunne tjekket slet ikke køre på de trin, der bruges nu.
- Anlæggets egen fugtføler (9-14 %) tælles ikke længere som en sund måling, og den bruges ikke som fugt i huset. Kondenstjekket i varmebalancen bruger rummenes fugt i stedet.

## Historik

- Nye kolonner: trin, kilde til trinnet, bypass, anlæggets egen frostsikring (til/fra og indblæsning), frostlås, anlæggets effekt, rummenes gennemsnitlige og laveste fugt, udefugt og højeste CO₂.
- T1-fejlen på 655 °C (17.-19. november) er fjernet fra de gemte data, og urimelige temperaturer bliver ikke længere gemt eller læst.
- Eftervarmens historikfelter markeres som ukendte, hvis styringen ikke har opdateret dem i 3 minutter (de stod fast i 56 timer 22.-24. november). Så lyser en info-alarm.
- Dagens udetemperatur og de seneste dages middel genopbygges fra historikken efter en genstart (bruger T1).
- Eftervarmens skala tæller først et trin som nået efter 6 minutter, ikke 3.
- Køl stopper allerede, når frostlåsen slår til. Nu står det også i ændringsloggen ("Køl stoppet: frostsikring").

## Ugerapport

- Ny blok "Komfort": minutter med CO₂ over 900 og 1000 ppm i det mest belastede rum, timer med tør luft (under 30 %), minutter med anlæggets egen frostsikring og varmegenvindingen som midterværdi. Felter, der ikke var gemt endnu, vises som ukendt og ikke som "fint".
- Beslutningsloggen gemmer nu alle årsager, der ville have mere luft (op til seks), ikke kun vinderen.

## Ikke med

- "Korrektion T3" overalt (ikke ændret, du valgte selv kun at bruge den i kanaltabet).
- Timerække over to år og fjernelse af de døde kolonner i historikken.
