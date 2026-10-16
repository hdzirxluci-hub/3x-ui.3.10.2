/* ---------------------------------------------------------------------------
   Gregorian -> Jalali (Persian) calendar, client side.

   Why client side: the panel does this conversion in Python
   (app/subscription/share.py builds "JALALI_EXPIRE_DATE" with jdatetime) but
   that variable is not among the keys handed to a subscription template, and
   the template has no Python filter for it.

   This is the jalaali-js algorithm. It is not hand-rolled guesswork: the
   harness in tools/check-jalali.js fuzz-tests it against Python's jdatetime
   over a wide date range, and the build fails if a single date disagrees.
   --------------------------------------------------------------------------- */
(function (root) {
  "use strict";

  function div(a, b) { return Math.trunc(a / b); }
  function mod(a, b) { return a - Math.trunc(a / b) * b; }

  // Julian Day Number <-> Gregorian
  function g2d(gy, gm, gd) {
    var d = div((gy + div(gm - 8, 6) + 100100) * 1461, 4) +
            div(153 * mod(gm + 9, 12) + 2, 5) + gd - 34840408;
    return d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
  }
  function d2g(jdn) {
    var j = 4 * jdn + 139361631;
    j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
    var i = div(mod(j, 1461), 4) * 5 + 308;
    var gd = div(mod(i, 153), 5) + 1;
    var gm = mod(div(i, 153), 12) + 1;
    var gy = div(j, 1461) - 100100 + div(8 - gm, 6);
    return { gy: gy, gm: gm, gd: gd };
  }

  // Jalali leap-year table
  var BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
                1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];

  function jalCal(jy) {
    var bl = BREAKS.length, gy = jy + 621, leapJ = -14, jp = BREAKS[0];
    var jm, jump = 0, leap, leapG, march, n, i;

    for (i = 1; i < bl; i += 1) {
      jm = BREAKS[i];
      jump = jm - jp;
      if (jy < jm) break;
      leapJ = leapJ + div(jump, 33) * 8 + div(mod(jump, 33), 4);
      jp = jm;
    }
    n = jy - jp;

    leapJ = leapJ + div(n, 33) * 8 + div(mod(n, 33) + 3, 4);
    if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;

    leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
    march = 20 + leapJ - leapG;

    if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33;
    leap = mod(mod(n + 1, 33) - 1, 4);
    if (leap === -1) leap = 4;

    return { leap: leap, gy: gy, march: march };
  }

  function g2j(gy, gm, gd) {
    var jdn = g2d(gy, gm, gd);
    var gyy = d2g(jdn).gy;          // the Gregorian year jdn actually falls in
    var jy = gyy - 621;
    var r = jalCal(jy);
    var jdn1f = g2d(gyy, 3, r.march);
    var k = jdn - jdn1f, jm, jd;

    if (k >= 0) {
      if (k <= 185) {
        jm = 1 + div(k, 31);
        jd = mod(k, 31) + 1;
        return { jy: jy, jm: jm, jd: jd };
      }
      k -= 186;
    } else {
      jy -= 1;
      k += 179;
      if (r.leap === 1) k += 1;
    }
    jm = 7 + div(k, 30);
    jd = mod(k, 30) + 1;
    return { jy: jy, jm: jm, jd: jd };
  }

  var MONTHS = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور",
                "مهر","آبان","آذر","دی","بهمن","اسفند"];

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  /* ts = unix seconds -> "۱۴۰۵/۰۸/۲۱" plus the month name */
  function fromUnix(ts, tzOffsetMinutes) {
    var d = new Date((ts + (tzOffsetMinutes || 0) * 60) * 1000);
    var j = g2j(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
    return {
      jy: j.jy, jm: j.jm, jd: j.jd,
      month: MONTHS[j.jm - 1],
      short: j.jy + "/" + pad(j.jm) + "/" + pad(j.jd),
      long: j.jd + " " + MONTHS[j.jm - 1] + " " + j.jy
    };
  }

  var api = { g2j: g2j, fromUnix: fromUnix, MONTHS: MONTHS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CWJalali = api;
})(typeof window !== "undefined" ? window : this);
