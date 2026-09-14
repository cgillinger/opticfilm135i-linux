# Uppdaterad kurs för `opticfilm135i-linux`

## Syfte

Det här är ett förslag till uppdaterad projektkurs för `cgillinger/opticfilm135i-linux`.

Behandla det inte som en mekanisk arbetsorder. Läs repot, den aktuella koden, testerna, dokumentationen och den senaste commit-historiken och bilda dig en egen uppfattning. Om evidensen i repot talar för en bättre ordning, en annan avgränsning eller att någon punkt här är fel, säg det och justera kursen.

Utgångspunkten är samtidigt ganska tydlig: projektet har passerat den fas där huvudfrågan är om vi kan få skannern att fungera under Linux. Det kan vi. Den egna Python-drivrutinen har nått sin definierade milstolpe, och SANE-backenden har fungerat på riktig hårdvara genom installation, scanning från `scanimage` och digiKam samt full load → scan → eject utan hjälp av Python-CLI.

Projektledningen bör därför flytta tyngdpunkten från **fortsatt generell utveckling och testning** till **konsolidering, reproducerbarhet och en möjlig SANE-leverans**.

Det betyder inte att koden är oantastlig eller att inga buggar återstår. Det betyder att nya aktiviteter nu bör behöva motiveras mot ett konkret återstående mål.

---

# 1. Börja med att fastställa nuläget själv

Innan du ändrar något: gör en kort men självständig nulägeskontroll.

Utgå inte från att README, ROADMAP eller detta dokument automatiskt är sanningen. Jämför dem med:

- aktuell `master`,
- senaste relevanta commits,
- `docs/test-log.md`,
- `docs/ROADMAP.md`,
- SANE-koden och dess tester,
- `docs/sane-wp3-submission.md`,
- den faktiska submission-branchen om den finns tillgänglig.

Identifiera särskilt motsägelser mellan:

1. vad koden faktiskt gör,
2. vad som har verifierats på hårdvara,
3. vad testerna bevisar,
4. vad dokumentationen säger är nuvarande status.

Om min föreslagna kurs nedan fortfarande håller efter denna kontroll, använd den. Om inte, förbättra den.

Vi vill ha en projektledare som resonerar, inte en exekveringsmotor som följer en gammal plan trots ny evidens.

---

# 2. Ny huvudprincip: sluta leta efter mer arbete bara för att arbete går att hitta

Projektets tidigare utvecklingsfas krävde aggressiv felsökning, reverse engineering och många kontroller. Det var rätt då.

Nu bör en annan regel dominera:

> En avslutad funktion öppnas inte igen utan konkret regressionsbevis, ny relevant felinformation eller en ändring som faktiskt påverkar den tidigare verifieringen.

"Vi skulle kunna testa mer" är inte längre ett tillräckligt skäl.

Nya tester ska kunna kopplas till:

- ett öppet krav,
- en konkret upstreamfråga,
- en faktisk regression,
- eller en förändring som behöver verifieras.

Undvik att skapa ett permanent projekt där Test 82 automatiskt leder till Test 83 därför att ytterligare ett test alltid är möjligt.

Samma slutdisciplin ska gälla granskningen inför submission. Efter nulägesgranskningen fastställer du en kort lista över konkreta submissionblockerare. För varje punkt anger du:

- vilket krav eller belagt problem den gäller,
- vilken åtgärd eller vilket beslut som behövs,
- vilket verifierbart slutkriterium som stänger punkten.

Övriga frågor dokumenteras som motiverade designval eller framtida arbete. En tänkbar reviewerinvändning är inte i sig en blockerare. Nya blockerare tillkommer endast med konkret evidens, exempelvis ett identifierat fel eller ett tillämpligt upstreamkrav. När slutkriterierna är uppfyllda går arbetet vidare till beslut om submission.

---

# 3. Föreslagen prioritet 1: gör repot självkonsistent

Det här bör sannolikt vara nästa konkreta arbetsområde.

Utvecklingen har gått snabbare än dokumentationen. Min genomgång visar åtminstone följande typ av drift:

- README beskriver fortfarande delar av SANE magazine handling som om de inte hade hardware-verifierats.
- Äldre delar av ROADMAP beskriver `load_document()` / `eject_document()` och CLI/SANE-uppdelningen på ett sätt som motsägs av senare WP-4-resultat.
- Samma ROADMAP säger samtidigt att den är "single source of truth".
- submissiondokumentet innehåller åtminstone någon limitation/statusformulering som har blivit inaktuell efter de senaste färg- och vendorjämförelserna.

Gör därför en **statuskonvergens**.

Målet är inte att skriva mer dokumentation. Målet är att minska mängden dokumentation som kan ge två olika svar på samma fråga.

### Föreslagen arbetsprincip

Aktuella statusdokument ska beskriva **nuet**.

Historiska slutsatser, inklusive sådant som senare visade sig vara fel, ska finnas kvar där de hör hemma — främst testlogg, analysdokument och git history — men inte fortsätta stå som aktuella påståenden i README eller den normativa delen av ROADMAP.

Undvik "UPDATE: detta är inte längre sant" staplat ovanpå gammal text när det går att skriva den aktuella beskrivningen korrekt direkt.

Efter detta steg ska en ny läsare kunna svara entydigt på:

- Är Python-drivrutinen klar inom sin scope?
- Fungerar SANE lokalt?
- Kan SANE själv ladda och mata ut magasinet?
- Vad är faktiskt hardware-verifierat?
- Vad återstår före en möjlig upstreamsubmission?
- Vilka saker är uttryckligen senare funktionalitet och inte blockers?

---

# 4. Föreslagen prioritet 2: gör offlinekvaliteten reproducerbar

Projektet har nu ett stort och värdefullt offline-testpaket. Det är dags att överväga om resultatet ska bli en automatisk egenskap hos varje relevant commit snarare än något vi manuellt konstaterar.

Min rekommendation är att du bedömer om en liten, ren CI-lösning bör införas.

Den får **inte** kommunicera med skannern.

En rimlig kandidat är att automatiskt verifiera sådant som:

- Python/offline-tester,
- generated-table consistency (`gen_sane_tables.py --check`),
- relevanta C++ probes,
- eventuellt ett reproducerbart compile-smoke-test för SANE-integrationen om det kan göras utan orimlig CI-komplexitet.

Men inför inte CI bara för att "alla projekt bör ha CI".

Bedöm:

- vad den faktiskt skulle skydda mot,
- vilka tester som är stabila och miljöoberoende,
- hur mycket underhåll den tillför.

Håll lösningen liten.

CI är ett möjligt sätt att uppfylla reproducerbarhetskravet, inte en egen obligatorisk milstolpe. Fastställ vilka offlinekontroller som behövs, deras beroenden, körkommandon och godkänt resultat. Ett dokumenterat och reproducerbart lokalt upplägg kan räcka om CI skulle tillföra oproportionerligt underhåll.

Om ett enkelt CI-upplägg ger betydande regressionsskydd är det motiverat. Om det kräver en stor specialmiljö och blir ett projekt i projektet, föreslå hellre en mindre variant.

---

# 5. Föreslagen prioritet 3: behandla SANE-upstream som nästa verkliga engineeringproblem

Jag ser inte längre scan/load/eject som den centrala osäkerheten.

Det som nu behöver granskas hårdast är sådant en extern SANE-maintainer rimligen kan reagera på.

Gör en maintainer-review av submissionen och rangordna verkliga risker.

Särskilt:

## A. Den genererade GL126-tabellen

`gl126_tables.cpp` är stor och genererad från material och generatorlogik som huvudsakligen finns i detta repo.

Det behöver finnas ett tekniskt försvarbart svar på frågor som:

- Varför behöver tabellerna vara checked in?
- Vilken del är faktisk statisk hårdvarudata och vilken del skulle kunna uttryckas semantiskt?
- Behöver generatorn följa med upstream?
- Behöver provenance eller reproducerbarhet förklaras bättre?
- Kan representationen rimligen minskas utan att återintroducera risk genom omfattande omskrivning?

Utgå inte från att filstorleken i sig är fel.

Undvik också att starta en stor refaktor bara för att göra submissionen estetiskt snyggare.

Målet är först att förstå vilken invändning en maintainer faktiskt skulle kunna ha och ha ett bra svar.

## B. Processlåset och persistent magazine state

SANE-backenden använder mekanik som andra genesys-enheter kanske inte behöver:

- processlås,
- persistent state mellan processer för det tvåstegade magasinflödet.

Det finns goda tekniska skäl till detta, men upstream kommer rimligen att vilja förstå dem.

Granska särskilt:

- stale-state-semantik,
- permissions/multi-user,
- crashbeteende,
- varför detta behöver finnas på disk,
- varför det inte kan lösas inom en vanlig backendprocess,
- vad som händer om companion-drivrutinen inte finns installerad.

Granska också säker filhantering för både låset och magasinmarkören. I den kod som granskades ligger standardlåset på en förutsägbar sökväg i `/tmp`; filerna öppnas utan `O_NOFOLLOW`, och skrivvägar kan trunkera den öppnade filen. Kontrollera först om detta fortfarande gäller och bedöm sedan:

- hantering av symboliska länkar och oväntade filtyper,
- ägarskap och skrivbehörigheter mellan användare,
- om kontroll och öppning kan påverkas av att sökvägen byts ut,
- hur fel eller ofullständig skrivning av markören hanteras.

Detta är en konkret granskningspunkt, inte ett konstaterande att en viss attack eller hårdvaruskada har visats. Skilj mellan designval som behöver förklaras och faktiska fel som behöver rättas. Verifiera berörda filoperationer offline i en isolerad testmiljö utan skanner.

Målet behöver inte vara att ta bort konstruktionen.

Målet är att den ska vara **avsiktlig, begriplig och reviewerbar**.

## C. De kvarvarande best-effort-pollerna

Cold-startarbetet har gett stark evidens för att vissa gamla väntetillstånd inte motsvarar något scannerstate som faktiskt uppnås på referensenheten.

Vi ska inte gissa nya registervillkor.

Men verifiera att kod och kommentarer gör skillnaden tydlig mellan:

- verkliga motor-completion waits,
- best-effort/diagnostic waits,
- villkor som sannolikt härrör från ett capture/transcription-antagande och därför tillåts timeouta.

En reviewer ska inte behöva reverse-engineera vår reverse engineering för att förstå varför en timeout är accepterad på ett ställe men fatal på ett annat.

## D. Påverkan på andra Genesys-enheter

Submissionen ändrar även gemensam Genesys-kod. Integrationspatchens ändring i `ImagePipelineNodeExtract` är ett konkret exempel på beteende som inte är avgränsat till GL126.

Identifiera vilka ändringar som är GL126-specifika och vilka som påverkar gemensamma kodvägar. För de gemensamma ändringarna:

- förklara vilket problem de löser och vilka beteenden som påverkas,
- kör relevanta befintliga offlinetester och komplettera endast där en konkret risk saknar täckning,
- bedöm om generella buggrättningar bör ligga i separata commits med egen motivering och verifiering.

Målet är ett avgränsat regressionsunderlag för den kod serien faktiskt ändrar. Det innebär inte en ny hårdvarukampanj för andra skannermodeller. Redovisa vad offlineunderlaget visar och vilken hårdvaruverifiering som saknas.

---

# 6. Föreslagen prioritet 4: kör upstreamrelevanta verifieringar — inte en ny allmän hardwarekampanj

Innan submission bör vi bedöma och sannolikt köra de SANE-projektspecifika tester som fortfarande saknas, särskilt:

- `scanimage -T`
- `tstbackend`

Men behandla dem som **hardware operations**, inte som oskyldiga lintverktyg.

Innan de körs:

1. förstå exakt vilka operationer de kommer att begära,
2. kontrollera om de kan trigga transport/scanning/cancel paths,
3. applicera projektets etablerade säkerhetsregler,
4. formulera stop conditions i förväg.

Ingen blind retry.

Ingen automatisk recovery efter en avvikelse.

Om ett verktyg visar sig göra något som våra säkerhetsgarantier inte kan bära, är ett dokumenterat "not run because..." bättre än att köra det bara för att fylla en checkruta.

---

# 7. Föreslagen prioritet 5: färdigställ submissiongrenen först när innehållet stabiliserats

Rebase inte tidigt om vi fortfarande gör substantiella förändringar i integrationspaketet.

När punkterna ovan är stabila:

1. fetch aktuell `sane-backends` master,
2. kontrollera om upstreamförändringar påverkar genesys eller våra patches,
3. rebase submissionserien,
4. lös konflikter som en riktig portning — kopiera inte bara gamla lösningar över ny upstreamkod,
5. bygg submissiongrenen fristående,
6. kör relevanta offlinekontroller mot **den faktiska submissiongrenen**, inte bara utvecklingsrepot,
7. kontrollera commitseriens kvalitet som om du vore extern reviewer.

Den färdiga serien ska gå att förstå utan att reviewern behöver känna till vårt utvecklingsrepo.

---

# 8. Beslutspunkt: prepared är inte submitted

Projektet har en viktig administrativ gräns.

Du får gärna:

- analysera upstream,
- skapa eller förbättra lokala branches,
- rebase,
- bygga,
- testa,
- formulera submissiontext,
- förbereda commits,
- föreslå exakt vad Christian bör skicka.

Du ska **inte** på eget initiativ:

- pusha till SANE eller någon annan extern/upstream-repository,
- öppna merge request,
- skapa issue hos upstream,
- skicka mail,
- kontakta maintainers.

AI får pusha till Christians eget repo om det ingår i ett uttryckligt uppdrag, men upstreamkontakt är en separat beslutspunkt för Christian.

B2 är därför två tillstånd:

**PREPARED:** tekniskt reviewerbart material finns.

**SUBMITTED:** Christian har uttryckligen beslutat att faktiskt skicka det och handlingen har utförts.

Blanda inte ihop dem för att få projektplanen att se färdig ut.

---

# 9. Saker som tills vidare bör hållas utanför huvudlinjen

Följande är intressanta och kan bli framtida arbete, men jag föreslår att de inte får blockera den nuvarande leveransen utan ny evidens:

### Whole-strip batch scanning i SANE

Det vore bra funktionalitet.

Det är inte nödvändigt för att visa att backend fungerar.

Om det tas upp senare bör implementationen följa SANE document-feeder-modell med upprepade `sane_start`, inte missbruka `last_frame`.

### Fysisk Eject-knapp / interrupt endpoint

Vi förstår nu mycket bättre hur vendorprogrammet använder EP `0x83`.

Det är ny funktionalitet och kan kräva förändringar i genesys USB-abstraktionen.

Inte ett submissionblocker för den scannerfunktion som redan fungerar.

### Slide holder

Kvarstår som det separata, beslutade målet C2 med ROADMAP:s befintliga acceptanskriterier. Förslaget är att lägga detta arbete efter den nuvarande SANE-submissionen; det tas inte bort ur projektet och blockerar inte B2. En ändring av själva omfattningen kräver ett uttryckligt scopebeslut.

### Panorama

Inte del av den nu verifierade vanliga strip-holder-funktionen.

### Färg-"fixar"

Inför ingen färgkorrigering bara för att en viss negativremsa ser gul ut.

Den senaste jämförelsen mot vendorsoftware förändrade evidensläget kraftigt och stödjer inte en driverfix.

Raw data är produkten; preview är sekundärt.

### Generell speed tuning

Cold-startens bevisade dödtid var värd att ta bort.

Det betyder inte att varje återstående paus automatiskt ska optimeras.

Ändra timing när vi vet vad vi väntar på och varför ändringen är säker.

### Fler repetitioner av redan accepterade profiler

Ingen testning för testningens egen skull.

---

# 10. Arbetsmetod jag föreslår att du använder

Du har stort mandat att resonera själv inom den här kursen.

När en fas är godkänd eller tydligt följer av uppdraget behöver du inte stanna och fråga inför varje reversibel filändring.

Men skilj strikt mellan:

### Normalt självständigt arbete

Exempel:

- läsa och jämföra dokument,
- korrigera motsägande status,
- skriva tester,
- göra targeted code fixes,
- bygga,
- köra offline-tester,
- analysera upstream,
- förbereda en lokal branch.

Gör detta färdigt inom den beslutade scopet.

### Beslut som Christian ska ta

Stanna före:

- nya eller förändrade hardwareexperiment som inte redan omfattas av en godkänd testplan,
- riskabla motorsekvenser,
- recoveryexperiment efter okänt hardware state,
- större scopeökning,
- upstreamkontakt/submission,
- förändringar som medvetet bryter en redan verifierad kompatibilitetsgaranti.

Om ett problem uppstår mitt i arbetet: gör allt annat som inte är blockerat innan du lämnar frågan till Christian.

---

# 11. Ändringsdisciplin

Föredra riktade ändringar framför stora omskrivningar.

Särskilt nu, när mycket av koden redan är hardware-verifierad, är "snyggare arkitektur" inte automatiskt ett tillräckligt skäl att röra fungerande transport- eller kalibreringskod.

Vid varje substantiell förändring, fråga:

1. Vilket konkret problem löser den?
2. Vilken tidigare verifiering påverkas?
3. Kan samma resultat nås med mindre förändring?
4. Behöver hårdvara återverifieras, eller räcker offlineevidens?
5. Är detta ett blocker för den nuvarande milstolpen eller bara ett framtida förbättringsförslag?

Lägg inte in opportunistiska fixes utanför scopet. Dokumentera dem som kandidater i stället.

---

# 12. Dokumentationsprincip

Projektets documentation bör framöver delas tydligare efter funktion:

**README**\
: Vad projektet är, vad som fungerar idag, hur en användare installerar/använder det och de viktigaste begränsningarna.

**ROADMAP**\
: Normativ projektstatus, definition of done, öppna respektive stängda milstolpar och framtida beslutad scope.

**test-log**\
: Historisk evidens. Även misslyckanden och felaktiga hypoteser får leva kvar här.

**analysdokument**\
: Resonemang, hypoteser, reverse engineering och detaljerad teknisk evidens.

**submissiondokument**\
: Vad en extern SANE-reviewer behöver veta.

Undvik att använda README som historisk logg eller testloggen som nuvarande status.

---

# 13. Föreslagen ny projektbild

Om din egen genomgång inte hittar evidens som motsäger detta föreslår jag att projektets övergripande status formuleras ungefär så här:

### A — Standalone Linux driver

**DONE inom definierad single-unit scope.**

Underhåll endast vid regression eller välavgränsad ny funktion.

### B1 — Lokal SANE-backend

**DONE.**

Backend har installerats och använts från vanliga SANE-vägar, och magazine handling har hardware-verifierats utan Python-CLI.

### B2 — SANE contribution

**PREPARATION PHASE.**

Den tekniska kärnan fungerar.

Återstående arbete handlar främst om submissionkvalitet, upstreamanpassning, reproducerbarhet och de sista relevanta verifieringarna.

### C1 — Strip holder, frames 1–6

Behåll ROADMAP:s befintliga acceptanskriterier och redovisa status separat för Python-drivrutinen och SANE-backenden. Ersätt den vaga sammanfattningen ”långt gången/färdig inom den verifierade delen” med ett entydigt besked per återstående kriterium: uppfyllt med evidenshänvisning, eller öppet med ett konkret slutkriterium.

Redan accepterade delar står kvar som avslutade. Nulägeskontrollen ska inte automatiskt skapa nya tester eller göra C1 till ett nytt B2-krav utöver den funktion submissionen faktiskt påstår sig stödja.

### C2 — Mounted-slide holder

**Kvarstående separat projektmål; föreslås genomföras efter B2.**

Behåll ROADMAP:s avgränsning: ett tomt originalmagasin kan verifiera identifiering, laddning, transport och geometri, men inte bildkvalitet och IR-beteende hos en fysisk dia. Den senare delen ska fortsatt anges som separat overifierad.

C2 tas inte bort när dokumentationen städas. Senareläggning ändrar arbetsordningen; en minskning av beslutade acceptanskriterier kräver Christians uttryckliga scopebeslut. Andra hållartyper får inte automatiskt återöppna A/B.

---

# 14. Definition av framgång för nästa fas

Nästa fas är lyckad när:

- README, ROADMAP och submissionunderlag berättar samma aktuella historia,
- de i nulägesgranskningen fastställda offlinekontrollerna går att reproducera med dokumenterade beroenden och kommandon, utan skanner,
- den avgränsade blockerlistans slutkriterier är uppfyllda och övriga granskningsfrågor har dokumenterade designbeslut eller är placerade i framtida arbete,
- filhanteringen för lås och magasinmarkör samt påverkan på gemensam Genesys-kod har granskats och eventuella blockerande fel har åtgärdats och verifierats,
- upstreamspecifika tester har körts när det är säkert eller dokumenterats ärligt när de inte kan köras,
- submissionserien bygger rent mot aktuell sane-backends,
- det inte finns något känt blockerande fel i den funktion som serien påstår sig stödja,
- Christian kan fatta ett enkelt beslut:

> "Skicka detta till SANE"\
> eller\
> "Skicka det inte ännu, av följande konkreta skäl."

Det ska inte längre behövas ett diffust svar av typen "vi kan nog testa lite till".

---

# 15. Din roll

Agera gärna mer som teknisk projektledare och senior reviewer än som en kodgenerator.

Jag vill att du utmanar den här planen om du hittar bättre evidens.

Om något jag föreslår är överarbete, säg det.

Om jag underskattar en risk, säg det.

Om en punkt redan är löst i repot, återöppna den inte för att den står här.

Om du hittar en verklig blocker som jag missat, prioritera den.

Men var också beredd att säga **"det här är tillräckligt"** när acceptancekriteriet faktiskt är uppfyllt.

Projektets nya problem är inte brist på möjliga arbetsuppgifter. Problemet är att välja vilka som fortfarande spelar roll.

## Första leverans från denna kurs

När du tar över från detta dokument vill jag att du först:

1. läser in det aktuella repot och den relevanta senaste historiken,
2. jämför verkligheten med denna föreslagna kurs,
3. ger Christian din egen korta bedömning av planen:
   - **ACCEPT**,
   - **ACCEPT WITH CHANGES**, eller
   - **RETHINK**,
4. specificerar de ändringar du anser behövs och varför,
5. presenterar den konkreta ordning du själv rekommenderar för nästa arbetsfas,
6. fastställer den korta blockerlistan med slutkriterier och vilka offlinekontroller som ska ingå, samt skiljer dessa från framtida arbete.

Var självsäker där evidensen är stark och explicit där något fortfarande är en bedömning.

Utför inte nya hardwareexperiment eller någon upstreamkontakt bara för att de nämns i detta dokument. De kräver respektive beslutspunkt.
