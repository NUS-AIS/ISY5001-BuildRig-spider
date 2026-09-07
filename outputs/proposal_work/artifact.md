# Proposal template editing contract

Reference: D:/009-course/ISS5001/Project_Proposal_Template.docx
SHA256: 82fad507a006829dccb3e93b148f9cbd5fcbb9c9c4ee127796adf315ec54f054
Reference preview: reference.pdf and reference-1.png/reference-2.png (2 pages).
The packaged LibreOffice renderer was attempted but no bundled LibreOffice exists on Windows. Word read-only PDF export supplies authoritative preview; pypdfium2 renders page images.

One A4 portrait section, 11907 x 16840 twips, 1440-twip margins, header/footer distances 720 twips. Preserve sectPr, centered PROJECT PROPOSAL title, Filing Ref header and NUS ISS footer artwork. Preserve original 9017-twip one-column TableGrid and six rows, including border properties. Normal type 12pt; table labels Arial Narrow bold. Added answers use Arial Narrow 12pt regular black. Short bold lead-ins may introduce description paragraphs. No new tables or graphics.

Slot map in word/document.xml, body/tbl[1], zero-based rows: 0 proposal date blank; 1 project title replace empty answer paragraph; 2 group and student details preserve blank; 3 sponsor details preserve blank; 4 background and objectives replace empty answer paragraphs; 5 project description replace empty answer paragraphs. Remove unused blank answer paragraphs in rows 4 and 5 to allow natural text flow. Preserve all other cell labels, blank slots, and paragraph structures. Page continuation is allowed for substantive answers.

All ZIP members except word/document.xml are preserve-only and must remain byte-identical. Preserve header/footer, images, relationships, styles, numbering, metadata and settings. The source file is never edited. Output is a separate completed DOCX in outputs. Check package comparison, unknown slots, section geometry, all inserted text, and every rendered page before delivery.

Content: English to match template. Proposed Singapore computer recommendation system using FastAPI and LangChain, RAG, desktop/laptop planning, evidence and review agents, deterministic budget and compatibility checks, source-backed recommendations and iterative adjustment. Clearly distinguish existing collection from proposed functionality. Do not select an unconfirmed model, vector database, frontend framework, sponsor, team, dates or measured outcomes.
