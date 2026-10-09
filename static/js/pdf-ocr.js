/*
 * Text recognition for scanned worksheets.
 *
 * A scanned PDF is just pictures of pages: pdf.js can show it, but there is no
 * text to select, so the tutor can't mark words or mistakes in it. The same goes
 * for pictures inside other PDFs, e.g. a screenshot of the exercise pasted into
 * tablet notes. Before such a PDF is uploaded we run OCR (Tesseract, English +
 * German) in the tutor's browser and add the recognised words to each page as
 * invisible text, the same way scanner software makes "searchable PDFs". Words
 * that are already text are left alone. The pages look unchanged.
 *
 * It runs in the browser on purpose: the web server is small (Render free/starter
 * instance, no system packages), and OCR is far too CPU-heavy to run there.
 *
 * Everything is shipped with the site (static/vendor/, no third-party CDN) and
 * only downloaded the first time a scanned PDF is uploaded.
 *
 *   const out = await PdfOcr.process(file, { pdfjs, vendor: "/static/vendor/", onProgress });
 *   // out.file   -> the PDF to upload (the original if nothing was done)
 *   // out.status -> "ok" | "has-text" | "too-many-pages" | "failed" | "not-pdf"
 */
(function () {
  "use strict";

  // Pages with fewer characters than this count as "no text layer".
  const MIN_CHARS_PER_PAGE = 20;
  // A page that has text is still recognised if a picture covers at least this
  // share of it: e.g. notes exported from a tablet, where the handwriting has
  // text but the pasted exercise statement is only a screenshot.
  const MIN_IMAGE_SHARE = 0.02;
  // A recognised word that overlaps text already on the page by this much is
  // left out, so nothing is added twice.
  const MAX_OVERLAP = 0.3;
  // OCR is ~2–5 s per page on a laptop; past this we'd rather not block the tutor.
  const MAX_OCR_PAGES = 20;
  // Render pages so the longer side is about this many pixels (≈ 250 dpi on A4).
  const TARGET_PX = 2900;
  // Recognised words below this confidence are usually noise (specks, grid
  // lines, scribbles); so are "words" made only of strokes and dots.
  const MIN_WORD_CONFIDENCE = 50;
  const NOISE = /^[|!¦\/\\_\-—–~+.,:;'"`°]+$/;

  const scripts = new Map();
  function loadScript(src) {
    if (!scripts.has(src)) scripts.set(src, new Promise((res, rej) => {
      const s = document.createElement("script"); s.src = src;
      s.onload = () => res();
      s.onerror = () => { scripts.delete(src); rej(new Error("load " + src)); };
      document.head.appendChild(s);
    }));
    return scripts.get(src);
  }

  const mul = (m, n) => [m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1], m[0] * n[2] + m[2] * n[3],
    m[1] * n[2] + m[3] * n[3], m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5]];

  // Area (in PDF points²) of the largest picture painted on the page.
  async function largestImageArea(pdfjs, page) {
    const OPS = pdfjs.OPS, list = await page.getOperatorList();
    const paint = new Set([OPS.paintImageXObject, OPS.paintInlineImageXObject, OPS.paintJpegXObject,
      OPS.paintImageXObjectRepeat].filter(x => x !== undefined));
    let ctm = [1, 0, 0, 1, 0, 0], max = 0;
    const stack = [];
    for (let i = 0; i < list.fnArray.length; i++) {
      const fn = list.fnArray[i], args = list.argsArray[i];
      if (fn === OPS.save) stack.push(ctm);
      else if (fn === OPS.restore) ctm = stack.pop() || ctm;
      else if (fn === OPS.transform) ctm = mul(ctm, args);
      else if (fn === OPS.paintFormXObjectBegin) { stack.push(ctm); if (args && args[0]) ctm = mul(ctm, args[0]); }
      else if (fn === OPS.paintFormXObjectEnd) ctm = stack.pop() || ctm;
      else if (paint.has(fn)) max = Math.max(max, Math.abs(ctm[0] * ctm[3] - ctm[1] * ctm[2]));
    }
    return max;
  }

  // Which pages (1-based) need recognising: no text at all, or text plus a
  // sizeable picture that may hold more text.
  async function pagesToRecognize(pdfjs, doc) {
    const out = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const tc = await page.getTextContent();
      const chars = tc.items.reduce((n, it) => n + (it.str || "").replace(/\s+/g, "").length, 0);
      if (chars < MIN_CHARS_PER_PAGE) { out.push(i); continue; }
      const vp = page.getViewport({ scale: 1 });
      if (await largestImageArea(pdfjs, page) >= MIN_IMAGE_SHARE * vp.width * vp.height) out.push(i);
    }
    return out;
  }

  // Boxes (canvas pixels, [x0, y0, x1, y1]) of the text already on the page.
  async function existingTextBoxes(pdfjs, page, vp) {
    const tc = await page.getTextContent();
    return tc.items.filter(it => it.str && it.str.trim()).map(it => {
      const t = pdfjs.Util.transform(vp.transform, it.transform);
      const h = Math.hypot(t[2], t[3]), w = it.width * vp.scale;
      return [t[4], t[5] - h, t[4] + w, t[5]];
    });
  }

  function overlapsExisting(w, boxes) {
    const { x0, y0, x1, y1 } = w.bbox, area = (x1 - x0) * (y1 - y0);
    if (area <= 0) return true;
    return boxes.some(b => {
      const ix = Math.min(x1, b[2]) - Math.max(x0, b[0]), iy = Math.min(y1, b[3]) - Math.max(y0, b[1]);
      return ix > 0 && iy > 0 && ix * iy >= MAX_OVERLAP * area;
    });
  }

  async function renderPage(page) {
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(TARGET_PX / Math.max(base.width, base.height), 5);
    const vp = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(vp.width); canvas.height = Math.ceil(vp.height);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    return { canvas, vp };
  }

  function wordsOf(blocks) {
    const words = [];
    (blocks || []).forEach(b => (b.paragraphs || []).forEach(p => (p.lines || []).forEach(l =>
      (l.words || []).forEach(w => { const t = (w.text || "").trim(); if (t && !NOISE.test(t) && w.confidence >= MIN_WORD_CONFIDENCE) words.push(w); }))));
    return words;
  }

  // Keep only characters the standard Helvetica font can encode (WinAnsi:
  // covers English and German incl. ä ö ü ß „ “ – …).
  function encodable(font, text) {
    const set = new Set(font.getCharacterSet());
    return Array.from(text).filter(ch => set.has(ch.codePointAt(0))).join("");
  }

  // Add each recognised word as invisible text (render mode 3) over the spot
  // where it appears on the page, stretched to the word's width.
  function addTextLayer(PDFLib, pdfPage, font, fontKey, vp, words) {
    const { pushGraphicsState, popGraphicsState, beginText, endText, setFontAndSize,
      setTextRenderingMode, TextRenderingMode, setTextMatrix, showText } = PDFLib;
    const ops = [pushGraphicsState(), beginText(), setTextRenderingMode(TextRenderingMode.Invisible)];
    const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
    const len = v => Math.hypot(v[0], v[1]);
    for (const w of words) {
      const text = encodable(font, w.text.trim());
      if (!text) continue;
      const { x0, y0, x1, y1 } = w.bbox;
      // Canvas pixels -> PDF user space (handles page rotation and crop box).
      const bl = vp.convertToPdfPoint(x0, y1), br = vp.convertToPdfPoint(x1, y1), tl = vp.convertToPdfPoint(x0, y0);
      const along = sub(br, bl), up = sub(tl, bl);
      const width = len(along), height = len(up);
      if (width < 1 || height < 1) continue;
      const u = [along[0] / width, along[1] / width], v = [up[0] / height, up[1] / height];
      // Size the em box to the word's height; put the baseline at the
      // descender line so selections cover the word like the printed text.
      const size = height / 1.05;
      const descent = 0.21 * size;
      const textWidth = font.widthOfTextAtSize(text, size);
      if (!textWidth) continue;
      const k = width / textWidth;
      ops.push(
        setFontAndSize(fontKey, size),
        setTextMatrix(u[0] * k, u[1] * k, v[0], v[1], bl[0] + v[0] * descent, bl[1] + v[1] * descent),
        showText(font.encodeText(text)),
      );
    }
    ops.push(endText(), popGraphicsState());
    pdfPage.pushOperators(...ops);
  }

  async function process(file, opts) {
    const { pdfjs, vendor, onProgress = () => {} } = opts;
    if (!/\.pdf$/i.test(file.name || "") && file.type !== "application/pdf") return { file, status: "not-pdf" };
    let worker = null;
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      // pdf.js may detach the buffer it's given, so hand it a copy.
      const doc = await pdfjs.getDocument({ data: bytes.slice() }).promise;
      const todo = await pagesToRecognize(pdfjs, doc);
      if (!todo.length) return { file, status: "has-text" };
      if (todo.length > MAX_OCR_PAGES) return { file, status: "too-many-pages", pages: todo.length };

      const base = new URL(vendor, location.href).href;
      onProgress({ step: "load" });
      await Promise.all([loadScript(base + "tesseract/tesseract.min.js"), loadScript(base + "pdf-lib/pdf-lib.min.js")]);
      const { PDFDocument, StandardFonts } = window.PDFLib;
      const out = await PDFDocument.load(bytes, { updateMetadata: false });
      const font = await out.embedFont(StandardFonts.Helvetica);
      worker = await window.Tesseract.createWorker(["deu", "eng"], 1, {
        workerPath: base + "tesseract/worker.min.js",
        corePath: base + "tesseract/core",
        langPath: base + "tesseract/lang",
        workerBlobURL: false,
      });

      let found = 0, recognized = 0;
      for (let n = 0; n < todo.length; n++) {
        onProgress({ step: "page", page: n + 1, pages: todo.length });
        const page = await doc.getPage(todo[n]);
        const { canvas, vp } = await renderPage(page);
        const { data } = await worker.recognize(canvas, {}, { text: false, blocks: true });
        canvas.width = canvas.height = 0;  // free the bitmap right away
        const existing = await existingTextBoxes(pdfjs, page, vp);
        const all = wordsOf(data.blocks), words = all.filter(w => !overlapsExisting(w, existing));
        recognized += all.length;
        if (!words.length) continue;
        const pdfPage = out.getPage(todo[n] - 1);
        pdfPage.node.normalize();  // wrap the page's own content in q/Q first
        const fontKey = pdfPage.node.newFontDictionary(font.name, font.ref);
        addTextLayer(window.PDFLib, pdfPage, font, fontKey, vp, words);
        found += words.length;
      }
      doc.destroy();
      // Everything recognised was already text (e.g. a scan that was OCR'd before).
      if (!found) return { file, status: recognized ? "has-text" : "no-text-found" };
      const saved = await out.save({ useObjectStreams: true });
      return { file: new File([saved], file.name, { type: "application/pdf" }), status: "ok", pages: todo.length };
    } catch (e) {
      // Encrypted or broken PDFs, out of memory, offline…: upload the original.
      console.warn("PDF text recognition failed", e);
      return { file, status: "failed" };
    } finally {
      if (worker) worker.terminate();
    }
  }

  window.PdfOcr = { process, MAX_OCR_PAGES };
})();
