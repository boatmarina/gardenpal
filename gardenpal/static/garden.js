/* GardenPal shared client utilities — loaded globally via base.html */
(function (G) {

  /* Compress a File/Blob to JPEG, max 1400px on the long edge, quality 0.82 */
  G.compressImage = async function compressImage(file) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () {
        var MAX = 1400, w = img.naturalWidth, h = img.naturalHeight;
        if (w > MAX || h > MAX) { var r = Math.min(MAX / w, MAX / h); w = Math.round(w * r); h = Math.round(h * r); }
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(img.src);
        c.toBlob(function (b) { resolve(b || file); }, 'image/jpeg', 0.82);
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
   * Render a grouped plant-search dropdown.
   * Results from the same genus are clustered; cultivars/varieties are indented
   * below their parent species. Library-saved plants float to the top.
   * onSelect(plant) is called when a result is clicked.
   */
  G.renderPlantDropdown = function renderPlantDropdown(dropdown, results, onSelect) {
    dropdown.innerHTML = '';
    if (!results.length) { dropdown.hidden = true; return; }

    var subRanks = { variety: 1, cultivar: 1, subspecies: 1, form: 1, hybrid: 1, infrahybrid: 1 };

    /* Group by genus (first word of scientific name) */
    var groups = Object.create(null);
    var genusOrder = [];
    results.forEach(function(p) {
      var genus = (p.scientific_name || '').split(' ')[0] || '';
      if (!groups[genus]) { groups[genus] = []; genusOrder.push(genus); }
      groups[genus].push(p);
    });

    function esc(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    genusOrder.forEach(function(genus) {
      var group = groups[genus];

      /* Library items first; then species before cultivars/varieties; then alpha */
      group.sort(function(a, b) {
        if (!!a.from_library !== !!b.from_library) return a.from_library ? -1 : 1;
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
        var html = (isSub ? '<span class="dd-indent">↳</span>' : '')
          + '<span class="dd-name">' + esc(name) + '</span>';
        if (sci && sci.toLowerCase() !== name.toLowerCase()) {
          html += '<small>' + esc(sci) + '</small>';
        }
        if (plant.from_library) html += '<span class="dd-saved">in library</span>';
        btn.innerHTML = html;
        if (isSub) btn.className = 'dd-sub';
        btn.addEventListener('click', function() { onSelect(plant); });
        dropdown.appendChild(btn);
      });
    });

    dropdown.hidden = false;
  };

}(window.GP = window.GP || {}));
