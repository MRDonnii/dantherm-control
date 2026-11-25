# Brugerfladen (fase 5) – ændringer af Claude AI

## Rettet
- "Hvad bestemmer blæseren" på mobil viser nu en almindelig sætning. Står blæseren på normaltrin, står der fx "Normaltrin 4 · luften er god i alle rum (højest CO₂ 715 ppm i Soveværelse, grænsen er 800)" i stedet for et rum, der ikke bestemmer noget. Støv har fået almindelig tekst.
- Rettelsen af føleren før varmefladen vises som "rettet 0,4 grader op", ikke som en temperatur. Temperaturforskelle står i "grader", og formlerne er væk.
- Dagene på 7- og 30-dages graferne skifter ikke en time (og en dag) efter skift fra sommertid.
- Toppen spørger ikke længere GitHub hvert minut i hver fane. Svaret deles mellem faner, tjekkes højst hvert 10. minut (hvert 15. minut og når fanen åbnes), og den gemte kanal (beta eller stable) bruges.
- Hvis man skifter periode i Historik, kan et langsomt svar for en ældre periode ikke længere overskrive den nye.
- Et tryk på forsiden (trin, funktion, bypass) rulles ikke kortvarigt tilbage af en hentning, der var i gang.
- Når forbindelsen til anlægget er tabt, står der "Ingen kontakt til anlægget", ventilatortallene skjules, og intet trin ser valgt ud, hvis trinnet er ukendt (før blev trin 3 vist).

## Nyt (kun visning, intet skal slås til)
- Historik: "Hvorfor skiftede blæseren?" med tid, trin, hvad der bestemte det og hvor længe det holdt.
- Rum: "Luften i rummene" viser CO₂, fugt og støv i hvert rum mod grænsen og hvilket rum der bestemmer lige nu.

## Tekster
- Eftervarmens ventil vises som trin 0-10, ikke "ventilåbning i %" (ventilen åbner hurtigt, så trin x 10 % passede ikke).
- Med Pi-styring står der "Trin nu" i stedet for "I HAC1", så en skiftende HAC1-temperatur ikke ligner en fejl.
- Eftervarmeanalysen tæller også tid med lukket ventil, hvor luften er for kold, og bruger det valgte bånd i stedet for fast ±0,5.
- Almindeligt dansk i stedet for fagord: "Automatik i anlægget / med rummene", "Kort boost", "Styres af", "Forbindelse til anlægget", fejlbeskeder med navn og tilladt område.
- Indstillingssiden er ikke ændret (fase 1), så "Smart Auto" og "Local Auto" kan stadig stå dér.

Den lille ændring i Pi'ens admin-API (`check_update` med `cached: true`) svarer fra hukommelsen i stedet for at hente fra GitHub.
