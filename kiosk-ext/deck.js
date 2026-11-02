// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Copyright (C) 2026 Consecrated Tech
// Inside a Google Slides deck framed by the eximaro screen. Now and then Google answers a
// published deck with a page of its own - "Google Docs encountered an error..." - sent as
// if all were well, and from outside the frame the screen cannot tell that from the deck.
// So this looks, and tells the screen what came: 'deck' once a slide has been drawn,
// 'error' for one of Google's error pages, or 'unknown' when it is neither after ten
// seconds. The frame's address goes along, so the screen can tell which of its loads the
// answer is about.
(function () {
  // Only a deck the screen frames itself: not a frame inside the deck, nor a deck shown
  // inside some other web page.
  if (window.parent === window || window.parent !== window.top) return;
  // A slide drawn by the viewer. (Its page frame is in the very first answer from Google,
  // before anything is drawn, so that alone says nothing.)
  var DRAWN = '[class*="punch-viewer-svgpage"] svg';
  // Google's error pages: the Docs one in any language, its plainer ones in English.
  var ERROR_PAGE = '[id^="af-error-page"]';
  var ERROR_WORDS = /encountered an error|That\u2019s an error|That's an error|file you have requested does not exist|unable to open the file/i;
  var tries = 0, told = false;
  function tell(state) {
    if (told) return;
    told = true;
    window.parent.postMessage({ eximaro: 'deck', state: state, href: location.href }, '*');
  }
  function look() {
    if (document.querySelector(DRAWN)) {
      // Once it has been painted, so the screen never puts up a frame still empty.
      requestAnimationFrame(function () { tell('deck'); });
      setTimeout(function () { tell('deck'); }, 500);
      return;
    }
    if (document.querySelector(ERROR_PAGE)
        || ERROR_WORDS.test((document.body && document.body.innerText) || '')) { tell('error'); return; }
    if (++tries < 40) { setTimeout(look, 250); return; }
    tell('unknown');
  }
  look();
})();
