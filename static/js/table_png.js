/* Dependency-free "download this table as a PNG" helper.
 * Draws an HTML <table> onto a canvas and exports image/png — no html2canvas,
 * so it works on the production host (which has no Pillow / image libraries).
 * Adds a title, subtitle and a "current as of" timestamp so a shared image
 * shows how up-to-date the figures are.
 *
 * High-DPI (v2.43): scale=4 — the canvas is drawn at 4x the size implied by
 * its logical padding/row-height/font measurements below, the same "render
 * big, let the viewer downscale" technique used for the server-rendered
 * budget-page PNGs (cashbook/services/goal_chart.py). A canvas is just a
 * pixel grid with no inherent "size": drawing text at a LARGER font size
 * onto a LARGER canvas produces real additional detail (the browser's own
 * text rasteriser draws each glyph bigger), it does not blur anything —
 * the blur previously reported came from there not being enough source
 * pixels for print/zoom, not from any compression (this was already true
 * before the JPEG→PNG change, since PNG is lossless either way).
 * toDataURL('image/png') has no way to embed a physical-DPI (pHYs) chunk —
 * a Canvas API limitation, not something this file can work around — so
 * unlike the Pillow pipeline this relies on pixel count alone for
 * perceived quality; 4x is generous enough that this is not a practical
 * limitation for a page-width table.
 *
 *   tableToPng('myTable', {title:'Group A', subtitle:'Jan–Jun 2026',
 *                          filename:'group-a', stamp:'Collections as of …'});
 */
(function () {
  function rowCells(tr) {
    return Array.prototype.map.call(tr.children, function (td) {
      var head = tr.parentElement.tagName === 'THEAD';
      var foot = tr.parentElement.tagName === 'TFOOT';
      return {
        text: (td.innerText || '').trim(),
        num: td.classList.contains('num') || td.classList.contains('u-right'),
        bold: head || foot || td.querySelector('strong') !== null
      };
    });
  }

  window.tableToPng = function (table, opts) {
    opts = opts || {};
    if (typeof table === 'string') { table = document.getElementById(table); }
    if (!table) { return; }
    var scale = 4;
    var pad = 18 * scale, rowH = 30 * scale, font = 13 * scale;
    var stamp = opts.stamp || ('Current as of ' + new Date().toLocaleString());
    var hasSub = !!opts.subtitle;
    var titleH = (hasSub ? 70 : 56) * scale;   // room for title + subtitle + timestamp

    var headRows = table.tHead ? Array.prototype.map.call(table.tHead.rows, rowCells) : [];
    var bodyRows = table.tBodies.length ? Array.prototype.map.call(table.tBodies[0].rows, rowCells) : [];
    var footRows = table.tFoot ? Array.prototype.map.call(table.tFoot.rows, rowCells) : [];
    var ncols = (headRows[0] || bodyRows[0] || []).length;
    if (!ncols) { return; }

    var meas = document.createElement('canvas').getContext('2d');
    meas.font = font + 'px sans-serif';
    var colW = []; for (var i = 0; i < ncols; i++) { colW.push(0); }
    headRows.concat(bodyRows, footRows).forEach(function (r) {
      r.forEach(function (c, i) { var w = meas.measureText(c.text).width; if (w > colW[i]) { colW[i] = w; } });
    });
    colW = colW.map(function (w) { return Math.ceil(w + 26 * scale); });
    var bodyW = colW.reduce(function (a, b) { return a + b; }, 0);
    // keep the title/timestamp from being clipped on narrow tables
    var titleText = (opts.title || 'Report');
    var stampW = 0;
    meas.font = 'bold ' + (19 * scale) + 'px Georgia, serif';
    stampW = Math.max(stampW, meas.measureText(titleText).width);
    meas.font = (11 * scale) + 'px sans-serif';
    stampW = Math.max(stampW, meas.measureText(stamp).width,
                      meas.measureText(opts.subtitle || '').width);
    var totalW = Math.max(bodyW, Math.ceil(stampW)) + pad * 2;
    var nRows = headRows.length + bodyRows.length + footRows.length;
    var totalH = titleH + nRows * rowH + pad * 2;

    var canvas = document.createElement('canvas');
    canvas.width = totalW; canvas.height = totalH;
    var ctx = canvas.getContext('2d');
    ctx.textBaseline = 'middle';
    ctx.fillStyle = '#f4f1e8'; ctx.fillRect(0, 0, totalW, totalH);

    ctx.textAlign = 'left';
    ctx.fillStyle = '#1f5f4f'; ctx.font = 'bold ' + (19 * scale) + 'px Georgia, serif';
    ctx.fillText(titleText, pad, pad + 14 * scale);
    ctx.fillStyle = '#7a7568'; ctx.font = (11 * scale) + 'px sans-serif';
    var sy = pad + 32 * scale;
    if (hasSub) { ctx.fillText(opts.subtitle, pad, sy); sy += 16 * scale; }
    ctx.fillStyle = '#9a8b5a'; ctx.fillText(stamp, pad, sy);

    var y = pad + titleH;
    function drawRow(cells, opt) {
      if (opt.headerBg) { ctx.fillStyle = '#1f5f4f'; ctx.fillRect(pad, y, bodyW, rowH); }
      else if (opt.zebra) { ctx.fillStyle = '#eceadf'; ctx.fillRect(pad, y, bodyW, rowH); }
      if (opt.topBorder) {
        ctx.strokeStyle = '#cfc9ba'; ctx.lineWidth = 2 * scale;
        ctx.beginPath(); ctx.moveTo(pad, y + 1); ctx.lineTo(pad + bodyW, y + 1); ctx.stroke();
      }
      var x = pad;
      cells.forEach(function (c, i) {
        ctx.font = (c.bold ? 'bold ' : '') + font + 'px sans-serif';
        ctx.fillStyle = opt.headerBg ? '#ffffff' : '#2b2b2b';
        if (c.num) { ctx.textAlign = 'right'; ctx.fillText(c.text, x + colW[i] - 13 * scale, y + rowH / 2); }
        else { ctx.textAlign = 'left'; ctx.fillText(c.text, x + 13 * scale, y + rowH / 2); }
        x += colW[i];
      });
      y += rowH;
    }
    headRows.forEach(function (r) { drawRow(r, { headerBg: true }); });
    bodyRows.forEach(function (r, idx) { drawRow(r, { zebra: idx % 2 === 1 }); });
    footRows.forEach(function (r) { drawRow(r, { topBorder: true }); });

    var link = document.createElement('a');
    link.download = (opts.filename || 'report') + '.png';
    link.href = canvas.toDataURL('image/png');
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
  };
})();
