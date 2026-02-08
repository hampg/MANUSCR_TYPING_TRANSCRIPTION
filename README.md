# ÁGNENTÍV KÉZ- ÉS GÉPÍRÁS ÁTÍRÓ PIPELINE ÷ RUNBOOK
Agentív kézírás–gépelés transzkripciós pipeline – RUNBOOK

Ez a RUNBOOK lépésről lépésre bemutatja, hogyan lehet egy agentív OCR + transzkripciós munkafolyamatot felállítani, futtatni, újraindítani és auditálható módon dokumentálni. A cél kifejezetten akadémiai kutatásra alkalmas, reprodukálható feldolgozás.

⸻

0. Előfeltételek

Rendszer
	•	macOS vagy Linux
	•	Python 3.8+ (ajánlott: 3.10+)
	•	Terminál használata

Külső eszközök
	•	pdftoppm (Poppler)

brew install poppler



Python csomagok

pip install openai


⸻

1. Projektkönyvtár inicializálása

A következő parancs a teljes fájlstruktúrát létrehozza egy üres mappában:

mkdir -p OA_PROJEKT/{
  input_pdfs,
  agent_state,
  work,
  output,
  logs,
  stubs,
  specs/prompts
}

Könyvtárak szerepe

Könyvtár	Funkció
input_pdfs/	Feldolgozandó PDF-ek
agent_state/	Agent állapot (resume / restart)
work/	Ideiglenes fájlok (PNG-k, page-level output)
output/	Összefűzött v1 / v2 transzkripciók
logs/	Futtatási logok
stubs/	Oldalszintű modellkimenetek (audit / replay)
specs/prompts/	Promptok külön fájlban


⸻

2. Prompt fájlok elhelyezése

A pipeline NEM tartalmaz hardcode-olt promptot. Ezek külön fájlokból töltődnek be.

Kötelező fájlok

specs/prompts/
├── diplomatic_transcription_prompt.md
└── normalization_prompt.md

Példa (diplomatikus átirat prompt)

You are a scholarly transcription assistant.

Task:
- Produce a *diplomatic transcription* of the manuscript page.
- Preserve original line breaks.
- Mark uncertain readings with [?].
- Mark illegible spans with [...].
- Do not modernize language.

Output format:
=== TRANSCRIPTION ===
(text)

=== META ===
{json}


⸻

3. PDF-ek előkészítése
	•	Minden fájl kiterjesztése legyen .pdf (kisbetű!)
	•	Kerüld a szóközöket (ajánlott)

mv "My Scan.PDF" my_scan.pdf

Másold a PDF-eket az input könyvtárba:

cp my_scan.pdf OA_PROJEKT/input_pdfs/


⸻

4. API kulcs beállítása

SOHA ne commitáld GitHubra.

Terminálban (csak az aktuális sessionre):

export OPENAI_API_KEY="sk-..."

Ellenőrzés:

python3 -c "import os; print('KEY_OK' if os.getenv('OPENAI_API_KEY') else 'NO_KEY')"


⸻

5. Futási módok (agentív szemlélet)

5.1 Dry run / stub-only (API nélkül)

python3 -u agent_transcribe.py input_pdfs \
  --project-root . \
  --lang hu \
  --no-hitl \
  --no-api \
  --stub-mode generate

Cél:
	•	pipeline-logika tesztelése
	•	fájlstruktúra ellenőrzése

⸻

5.2 Éles futás, auditálhatóan (ajánlott)

python3 -u agent_transcribe.py input_pdfs \
  --project-root . \
  --lang hu \
  --no-hitl \
  --stub-mode record

Ez:
	•	valódi modellhívást végez
	•	minden oldal kimenetét eltárolja stubs/ alá
	•	megszakítható és újraindítható

⸻

6. Futás monitorozása

Élő log:

tail -f logs/*/run.log

Fut-e még a folyamat?

ps aux | grep agent_transcribe.py | grep -v grep


⸻

7. Újraindítás / resume / reset

Resume (automatikus)

Egyszerűen futtasd újra:

python3 -u agent_transcribe.py input_pdfs --project-root . --lang hu --no-hitl --stub-mode record

Teljes reset (új futás ugyanazzal a PDF-fel)

rm -f agent_state/*.state.json
rm -rf work/* output/* logs/*


⸻

8. TXT fájlok összefűzése (page-level ➜ egy szöveg)

A diplomatikus transzkripció során az agent oldalanként külön TXT fájlokat hoz létre a következő helyen:

work/<source_id>/diplomatic/
├── <source_id>_p001.txt
├── <source_id>_p002.txt
├── ...

A RUNBOOK alapértelmezésben automatikus összefűzést végez a futás végén (assemble_v1), amely létrehozza a teljes v1 állományt:

output/<source_id>/<source_id>_diplomatic_v1.txt

Ez a fájl az egyetlen, kanonikus hivatkozási pont a diplomatikus átirathoz.

⸻

8.1 Manuális összefűzés (ellenőrzéshez / reprodukcióhoz)

Akadémiai környezetben gyakran elvárás, hogy a lépések külön is reprodukálhatók legyenek. Az alábbi minimális Python-script pontosan ugyanazt csinálja, mint az agenten belüli automatikus lépés.

concat_pages.py

from pathlib import Path

SOURCE_ID = "<source_id>"  # pl. Odry03_copy_8a7df7a1
BASE = Path("work") / SOURCE_ID / "diplomatic"
OUT = Path("output") / SOURCE_ID / f"{SOURCE_ID}_diplomatic_v1_MANUAL.txt"

parts = []
parts.append(f"=== SOURCE: {SOURCE_ID} ===\n")

for txt in sorted(BASE.glob(f"{SOURCE_ID}_p*.txt")):
    page = txt.stem.split("_p")[-1]
    parts.append(f"\n=== PAGE {int(page)} ===\n")
    parts.append(txt.read_text(encoding="utf-8").rstrip() + "\n")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("".join(parts), encoding="utf-8")

print(f"Wrote: {OUT}")

Futtatás:

python3 concat_pages.py

Ez hasznos:
	•	agent output ellenőrzéséhez
	•	módszertani audit során
	•	kézi beavatkozás utáni újraösszefűzéshez

⸻

9. Kimenetek értelmezése

v1 – diplomatikus átirat

output/<source_id>/<source_id>_diplomatic_v1.txt

v2 – normalizált szöveg

output/<source_id>/<source_id>_corrected_v2.txt

Edit log (akadémiai átláthatóság)

output/<source_id>/<source_id>_editlog_v2.json


⸻

9. Agentív jellemzők (módszertani megjegyzés)

Ez nem egy sima script:
	•	perzisztens állapot (state)
	•	oldalszintű döntések
	•	reprodukálható stub replay
	•	ember nélküli batch futás
	•	auditálható prompt- és outputkezelés

Ezért alkalmas:
	•	filológiai / történeti kutatásra
	•	módszertani melléklethez
	•	GitHub-repozitóriumba publikálásra

⸻

10. Következő bővítési pontok (opcionális)
	•	timeout + retry policy
	•	TEI-XML export
	•	multi-PDF batch queue
	•	költséglogolás
	•	HITL checkpoint fájlok

⸻

Ez a RUNBOOK élő dokumentum. Javasolt minden módszertani változtatást commitként rögzíteni.
