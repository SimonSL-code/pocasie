# Počasie na Slovensku – merania staníc SHMÚ

Webová stránka, ktorá zobrazuje aktuálne merania automatických meteorologických
staníc Slovenského hydrometeorologického ústavu. Dáta si sama sťahuje z otvoreného
dátového servera SHMÚ <https://opendata.shmu.sk/> (priečinok
`meteorology/climate/now/data`), kde pribúda nový súbor každých 5 minút.

Na stránke nájdete:

- extrémy z posledného merania (najteplejšie, najchladnejšie, najsilnejší náraz vetra, najviac zrážok za hodinu),
- mapu Slovenska s teplotou na staniciach so známou polohou,
- tabuľku všetkých asi 95 staníc s vyhľadávaním a zoraďovaním,
- detail vybranej stanice: teplota, vlhkosť, vypočítaný rosný bod, tlak, vietor a nárazy, zrážky za 1 a 24 hodín,
  dohľadnosť, sneh, globálne žiarenie, prízemná teplota, teplota pôdy a stav počasia,
- grafy teploty, vetra, zrážok, tlaku a vlhkosti za posledných 24 hodín,
- mapy predpovede modelu ALADIN na 3 až 4 dni: teplota, zrážky, oblačnosť, vietor, nárazy a tlak,
  s posuvníkom času a prehrávaním; na GitHube aj mapy modelu ICON-EU na 5 dní,
- graf predpovede pre vybranú stanicu, ktorý porovnáva modely ALADIN, ICON-EU a ECMWF
  (teplota, súčet zrážok, nárazy vetra),
- vaša vlastná stanica z Weather Underground a až 20 amatérskych staníc v jej okolí,
  s vlastnou značkou na mape a kontrolou kvality.

Stránka sa obnovuje sama každú minútu, server kontroluje nové merania každé 2 minúty
a nový beh modelu ALADIN každých 20 minút.

## Stránka na internete cez GitHub (bez zapnutého počítača)

Stránka môže bežať aj zadarmo na GitHub Pages: <https://simonsl-code.github.io/pocasie/>.
Workflow `.github/workflows/pocasie.yml` asi každých 10 minút spustí na počítačoch GitHubu skript
`generuj.py`. Ten stiahne nové merania zo SHMÚ, pri novom behu aj predpoveď ALADIN, a zverejní
stránku. Stav medzi behmi (história pre grafy) sa ukladá do vetvy `stav`, ktorá sa zakaždým prepíše.

Zapnutie (stačí raz):

1. V repozitári otvorte **Settings → Pages** a v časti *Build and deployment* nastavte
   **Source: GitHub Actions**.
2. Otvorte **Actions → Aktualizácia počasia → Run workflow**. Prvý beh trvá asi 2 minúty, potom sa
   stránka obnovuje sama.

Vlastná stanica z Weather Underground (nepovinné): **Settings → Secrets and variables → Actions**

- na karte *Secrets* pridajte `WU_API_KEY` s kľúčom k API,
- na karte *Variables* pridajte `WU_STATION` s ID stanice, napr. `IBRATI123`,
- voliteľne `WU_DALSIE` s ďalšími ID staníc oddelenými čiarkou.

Obmedzenia oproti lokálnemu serveru:

- dáta sa obnovujú asi každých 10 minút; pri vyťažení GitHub spustenie oneskorí,
- stránka je verejná (kto pozná adresu, vidí ju); kľúč k API zostáva skrytý v *Secrets*,
- grafy za 24 hodín sa po prvom spustení dopĺňajú postupne počas niekoľkých behov,
- ak by GitHub automatické spúšťanie po dlhšej nečinnosti vypol, zapnete ho v záložke Actions.

## Čo potrebujete

Python 3.9 alebo novší. Žiadne ďalšie knižnice netreba.

- **Windows:** nainštalujte Python z <https://www.python.org/downloads/> (pri inštalácii zaškrtnite „Add Python to PATH“).
- **macOS a Linux:** Python 3 je zvyčajne už nainštalovaný (`python3 --version`).

## Spustenie

1. Rozbaľte priečinok `pocasie-shmu`.
2. Spustite:
   - **Windows:** dvojklik na `spustit.bat`
   - **macOS:** dvojklik na `spustit.command` (pri prvom spustení: pravý klik → Otvoriť)
   - **Linux alebo terminál:** `python3 server.py`
3. Otvorte <http://localhost:8000> (skripty `spustit` ju otvoria samy).

Okno s bežiacim serverom nechajte otvorené. Ukončíte ho klávesmi Ctrl+C alebo zatvorením okna.

Pri štarte server stiahne históriu za posledných 24 hodín: posledné 2 hodiny úplne, staršie po
30 minútach (spolu približne 70 súborov po 0,4 MB). Prvé údaje sa zobrazia do niekoľkých sekúnd,
grafy sa doplnia do minúty. Kým server beží, história sa dopĺňa úplne.

## Voľby

```
python server.py --port 8080          # iný port
python server.py --host 0.0.0.0       # stránka dostupná aj z iných zariadení v domácej sieti
python server.py --hodiny 48          # dlhšia história v grafoch
python server.py --interval 300       # kontrolovať nové dáta každých 5 minút
python server.py --bez-predpovede     # vypnúť predpoveď ALADIN (šetrí asi 11 MB sťahovania na každý beh)
python server.py --help               # všetky voľby
```

S voľbou `--host 0.0.0.0` otvoríte stránku z mobilu na adrese `http://<IP adresa počítača>:8000`.
Ak ju chcete mať verejne na internete, spustite ju na serveri (napr. VPS) za webovým serverom
ako nginx alebo Caddy; samotný `server.py` je jednoduchý a nemá prihlasovanie ani HTTPS.

## Ako čítať údaje

- **Časy** sú na stránke v miestnom čase. SHMÚ uvádza čas merania v SEČ (stále UTC+1, aj v lete);
  server ho prepočítava.
- **Zrážky:** SHMÚ zverejňuje minútové úhrny. Úhrn za 1 h a 24 h je súčet minút, ktoré má server
  k dispozícii. Ak chýba časť intervalu (napríklad tesne po štarte), hodnota má znak ≈ a pri detaile
  stanice je uvedené, z akej časti intervalu pochádza.
- **Tlak** je tlak v nadmorskej výške stanice, nie prepočítaný na hladinu mora. Preto majú horské
  stanice výrazne nižšie hodnoty.
- **Rosný bod** stránka vypočítava z teploty a vlhkosti (Magnusov vzorec).
- **Stav počasia** je kód podľa tabuľky WMO 4680, ktorý hlási len časť staníc.
- Stanica bez merania viac ako hodinu je označená ako **neaktuálna** a nezapočítava sa do extrémov.

## Predpoveď ALADIN

SHMÚ zverejňuje model ALADIN so sieťou 4,5 km štyrikrát denne (behy o 00, 06, 12 a 18 UTC).
Server si vyberie najnovší beh, ktorý má súvislé kroky aspoň do +72 hodín, a stiahne ho
(asi 60 súborov, spolu približne 11 MB). Beh o 00 UTC siaha až do +102 hodín. Do +48 hodín
sú kroky hodinové, ďalej po 3 hodinách.

- **Zrážky** sú zobrazené ako intenzita v mm za hodinu za interval pred zobrazeným časom.
- **Tlak** v predpovedi je prepočítaný na hladinu mora, na rozdiel od meraní na staniciach.
- **Teplota** platí pre terén modelu, ktorý je vyhladený. Na horských staniciach preto môže
  byť skutočná teplota iná.

## Ďalšie modely: ICON-EU a ECMWF

- **Mapa ICON-EU** (Nemecká meteorologická služba DWD, sieť 7 km, do +120 hodín) je len na stránke
  z GitHubu. `generuj.py` ju sťahuje z bezplatného [Open-Meteo](https://open-meteo.com/) po každom
  behu o 00, 06, 12 a 18 UTC. Na mape sa zobrazuje každý druhý bod siete (po 0,125°, asi 9 až 14 km),
  ktorý stačí na plynulú mapu a šetrí denný limit Open-Meteo. Modely s jemnejšou sieťou (ICON-D2,
  MeteoSwiss ICON-CH1/CH2) celé Slovensko nepokrývajú.
- **Graf pre stanicu** si údaje ICON-EU a ECMWF IFS (sieť 9 km) načíta priamo v prehliadači
  z Open-Meteo. Teplotu Open-Meteo prepočíta na nadmorskú výšku stanice, ALADIN ukazuje hodnotu
  najbližšieho bodu modelu. Ak Open-Meteo nie je dostupné, graf ukáže len ALADIN.
- Open-Meteo je zadarmo na nekomerčné použitie, údaje sú pod licenciou CC BY 4.0.
- Sťahovanie mapy ICON vypnete voľbou `python3 generuj.py --bez-icon`.

## Vlastná stanica z Weather Underground

Ak máte stanicu, ktorá posiela dáta na Weather Underground, stránka ju zobrazí spolu so
stanicami v okolí.

1. Prihláste sa na <https://www.wunderground.com/member/api-keys> a skopírujte kľúč k API
   (32 znakov). Kľúč dostane zadarmo každý, kto má na Weather Underground aktívnu stanicu.
2. Na stránke v časti „Moja stanica na Weather Underground“ vyplňte kľúč a ID svojej stanice
   (napr. IBRATI123) a kliknite na „Uložiť a pripojiť“.

Stránka potom pridá aj až 10 najbližších staníc a stanice, ktorých ID zadáte ručne (najviac 20).
Na mape majú tvar kosoštvorca; tlačidlom „Priblížiť okolie mojej stanice“ si zobrazíte len okolie.

- **Limit:** kľúč má limit 1 500 volaní denne. Vlastná stanica sa obnovuje každých 5 minút,
  ostatné tak, aby sa za deň minulo najviac asi 1 300 volaní (pri 10 staniciach každých 15 minút).
  Ak ten istý kľúč používa aj iná aplikácia, limit sa delí.
- **Kľúč** sa ukladá len do súboru `wunderground.json` v tomto priečinku a do prehliadača sa
  už neposiela. Meniť nastavenia možno len z prehliadača na počítači, kde server beží.
  Tlačidlo „Odpojiť“ súbor zmaže.
- **Kontrola kvality:** amatérske stanice často stoja na slnku alebo pri stene domu. Stanica sa
  označí ako podozrivá, ak sa jej teplota líši o viac ako 5 °C od najbližšej stanice SHMÚ do 30 km
  a zároveň od modelu ALADIN, alebo ak ju za chybnú označí sám Weather Underground. Amatérske
  stanice sa nezapočítavajú do extrémov v hornej časti stránky.
- **Tlak** amatérske stanice väčšinou udávajú prepočítaný na hladinu mora, na rozdiel od SHMÚ.

## Názvy staníc

SHMÚ v otvorených dátach označuje stanice len číselným indexom (`ind_kli`) a zoznam staníc
v strojovo čitateľnej podobe nezverejňuje. Súbor `stanice.json` preto obsahuje názvy a súradnice
94 staníc: 27 synoptických aj s nadmorskou výškou a 67 ďalších automatických staníc z mapy
na shmu.sk (stav k 13. 10. 2026). Všetky sa zobrazia na mape aj v tabuľke. Stanica, ktorú v zozname
nemáme, sa ukáže ako „Stanica č. …“.

Keď SHMÚ pridá novú stanicu, zoznam obnovíte takto:

1. V prehliadači otvorte <https://www.shmu.sk/sk/?page=838&uhrny=24> a uložte ju (Ctrl+S / Cmd+S)
   ako „Webová stránka, iba HTML“.
2. Spustite `python aktualizovat_stanice.py "cesta/k/ulozenej-strane.html"`.

Skript pridá chýbajúce stanice a doplní súradnice; názvy, ktoré už v súbore sú, nechá tak
(prepísať ich môžete voľbou `--prepisat-nazvy`). Stanicu môžete doplniť aj ručne v rovnakom tvare:

```json
"11816": { "nazov": "Bratislava - Letisko", "lat": 48.1717, "lon": 17.2, "vyska": 131 }
```

Server si zmeny v súbore načíta sám, netreba ho reštartovať.

## Riešenie problémov

- **„Port 8000 sa nedá použiť“** – port používa iný program, spustite `python server.py --port 8080`.
- **Chyba certifikátu (SSL)** – server opendata.shmu.sk neposiela celý reťazec certifikátov, preto je
  chýbajúci medzicertifikát pribalený v `certs/shmu-chain.pem`. Ak chyba pretrváva na macOS, spustite
  v priečinku Pythonu `Install Certificates.command` alebo `pip install certifi`.
- **Stránka hlási staré dáta** – SHMÚ občas mešká so zverejnením. Server to skúša znova sám.

## Obsah priečinka

| Súbor | Na čo slúži |
| --- | --- |
| `server.py` | sťahuje dáta zo SHMÚ a poskytuje webovú stránku |
| `static/index.html` | samotná stránka (HTML, CSS, JavaScript v jednom súbore) |
| `static/hranice.json` | štátne hranice pre mapu |
| `stanice.json` | názvy a polohy staníc |
| `aktualizovat_stanice.py` | doplní stanice do `stanice.json` z uloženej stránky SHMÚ |
| `generuj.py` | statická verzia pre GitHub Pages (spúšťa ju GitHub Actions) |
| `.github/workflows/pocasie.yml` | plán spúšťania na GitHube každých 10 minút |
| `aladin.py` | číta súbory predpovede ALADIN (formát GRIB2) bez ďalších knižníc |
| `wunderground.py` | sťahuje vlastnú a okolité stanice z Weather Underground |
| `wunderground.json` | vznikne po zadaní kľúča: kľúč a ID stanice (nikomu ho neposielajte) |
| `certs/shmu-chain.pem` | medzicertifikát a koreňový certifikát Sectigo pre opendata.shmu.sk |
| `spustit.bat`, `spustit.command` | spustenie dvojklikom |

## Zdroje a licencie

- Meteorologické dáta: Slovenský hydrometeorologický ústav, <https://opendata.shmu.sk/>.
- Názvy a súradnice ostatných automatických staníc: mapa staníc SHMÚ, <https://www.shmu.sk/sk/?page=838&uhrny=24>.
- Formát dát, súradnice synoptických staníc a pozorovania o časovom pásme vychádzajú z projektu
  [vaind/ha-shmu](https://github.com/vaind/ha-shmu) (licencia MIT, © 2026 Ivan Dlugoš).
- Geometria mriežky a čísla parametrov modelu ALADIN vychádzajú z toho istého projektu; čítanie
  súborov GRIB2 bolo overené voči knižnici ecCodes (ECMWF).
- Štátne hranice: Natural Earth 1:10m, voľné dielo (public domain).
- Modely ICON-EU (DWD) a ECMWF IFS: [Open-Meteo.com](https://open-meteo.com/), licencia
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
