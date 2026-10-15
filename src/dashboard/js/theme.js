(function () {
    'use strict';
    var theme = 'dark';
    try {
        if (localStorage.getItem('gideon-theme') === 'light') theme = 'light';
    } catch (e) { /* storage unavailable */ }
    document.documentElement.setAttribute('data-theme', theme);
})();
