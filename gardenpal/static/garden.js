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

}(window.GP = window.GP || {}));
