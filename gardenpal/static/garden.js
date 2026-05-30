/* GardenPal shared client utilities — loaded globally via base.html */
(function (G) {

  /* Compress a File/Blob to JPEG. maxPx defaults to 1400, quality to 0.82 */
  G.compressImage = async function compressImage(file, maxPx, quality) {
    maxPx = maxPx || 1400;
    quality = quality != null ? quality : 0.82;
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () {
        var MAX = maxPx, w = img.naturalWidth, h = img.naturalHeight;
        if (w > MAX || h > MAX) { var r = Math.min(MAX / w, MAX / h); w = Math.round(w * r); h = Math.round(h * r); }
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(img.src);
        c.toBlob(function (b) { resolve(b || file); }, 'image/jpeg', quality);
      };
      img.onerror = function () { resolve(file); };
      img.src = URL.createObjectURL(file);
    });
  };

  /* Read a Blob/File as a base-64 data URL */
  G.blobToDataURL = function blobToDataURL(blob) {
    return new Promise(function (resolve) {
      var fr = new FileReader();
      fr.onload = function () { resolve(fr.result); };
      fr.readAsDataURL(blob);
    });
  };

  /*
   * Fetch plant photos and details in parallel.
   * Returns { photos: string[], details: object|null }
   * photos is sliced to 3; details is null on error.
   */
  G.fetchPlantData = async function fetchPlantData(name, photoCount) {
    photoCount = photoCount || 6;
    var q = encodeURIComponent(name);
    var results = await Promise.all([
      fetch('/api/plant-photos?q=' + q + '&count=' + photoCount)
        .then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch('/api/plant-details?q=' + q)
        .then(function (r) { return r.json(); }).catch(function () { return {}; })
    ]);
    return {
      photos:  (results[0].photos || []).slice(0, 3),
      details: (results[1] && !results[1].error) ? results[1] : null
    };
  };

  /*
   * Render a grouped, scored plant-search dropdown.
   *
   * Ordering priority (highest wins):
   *   4 — exact match on common or scientific name
   *   3 — plant is already in the user's library
   *   2 — name starts with the query (prefix match)
   *   1 — everything else (preserves iNaturalist's relevance order within tier)
   *
   * Results are sorted by score before genus-grouping, so the genus of the
   * best result appears first. Within each genus group cultivars/varieties are
   * indented below their parent species.
   *
   * query  — the raw search string typed by the user (used for scoring)
   * onSelect(plant) is called when a result is clicked.
   */
  G.renderPlantDropdown = function renderPlantDropdown(dropdown, results, onSelect, query) {
    dropdown.innerHTML = '';
    if (!results.length) { dropdown.hidden = true; return; }

    var subRanks = { variety: 1, cultivar: 1, subspecies: 1, form: 1, hybrid: 1, infrahybrid: 1 };
    var qL = (query || '').toLowerCase().trim();

    function scoreResult(plant) {
      var n = (plant.common_name    || '').toLowerCase().trim();
      var s = (plant.scientific_name || '').toLowerCase().trim();
      if (qL && (n === qL || s === qL))            return 4;  // exact match
      if (plant.from_library)                       return 3;  // in user's library
      if (qL && (n.startsWith(qL) || s.startsWith(qL))) return 2;  // prefix match
      return 1;                                                // general API match
    }

    function esc(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    /* Sort by score before grouping so the best-matching genus appears first.
       Stable: same-score results keep their original (iNaturalist relevance) order. */
    var scored = results.map(function(p, i) { return { p: p, s: scoreResult(p), i: i }; });
    scored.sort(function(a, b) { return b.s !== a.s ? b.s - a.s : a.i - b.i; });
    var sorted = scored.map(function(x) { return x.p; });

    /* Group by genus (first word of scientific name), preserving score order */
    var groups = Object.create(null);
    var genusOrder = [];
    sorted.forEach(function(p) {
      var genus = (p.scientific_name || '').split(' ')[0] || '';
      if (!groups[genus]) { groups[genus] = []; genusOrder.push(genus); }
      groups[genus].push(p);
    });

    genusOrder.forEach(function(genus) {
      var group = groups[genus];

      /* Within a group: score first, then species before cultivars, then alpha */
      group.sort(function(a, b) {
        var sa = scoreResult(a), sb = scoreResult(b);
        if (sa !== sb) return sb - sa;
        var aS = !!subRanks[a.rank || ''], bS = !!subRanks[b.rank || ''];
        if (aS !== bS) return aS ? 1 : -1;
        return (a.scientific_name || '').localeCompare(b.scientific_name || '');
      });

      /* Only indent sub-rank entries when a species-level result is also in the group */
      var hasSpecies = group.some(function(p) { return !subRanks[p.rank || '']; });

      group.forEach(function(plant) {
        var isSub = !!subRanks[plant.rank || ''] && hasSpecies && group.length > 1;
        var btn = document.createElement('button');
        btn.type = 'button';
        var name = plant.common_name || plant.scientific_name || '';
        var sci  = plant.scientific_name || '';
        var photoUrl = (plant.taxon_photos && plant.taxon_photos[0]) || plant.photo_url || '';
        var textHtml = '<span class="dd-name">' + esc(name) + '</span>';
        if (sci && sci.toLowerCase() !== name.toLowerCase()) {
          textHtml += '<small>' + esc(sci) + '</small>';
        }
        if (plant.from_library) textHtml += '<span class="dd-saved">in library</span>';
        var html = '';
        if (photoUrl) html += '<img class="dd-thumb" src="' + esc(photoUrl) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'" />';
        if (isSub) html += '<span class="dd-indent">↳</span>';
        html += '<span class="dd-text">' + textHtml + '</span>';
        btn.innerHTML = html;
        if (isSub) btn.className = 'dd-sub';
        btn.addEventListener('click', function() { onSelect(plant); });
        dropdown.appendChild(btn);
      });
    });

    dropdown.hidden = false;
  };

}(window.GP = window.GP || {}));
