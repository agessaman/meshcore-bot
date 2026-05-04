/**
 * Shared channel slot + API helpers for web viewer (Feeds modal, Radio page).
 * Exposes window.MeshCoreChannelOps
 */
(function (global) {
    'use strict';

    function getLowestAvailableChannelIndex(channels, maxChannels) {
        var used = new Set((channels || []).map(function (c) {
            return c.channel_idx !== undefined ? c.channel_idx : c.index;
        }));
        var max = typeof maxChannels === 'number' ? maxChannels : 40;
        for (var i = 0; i < max; i++) {
            if (!used.has(i)) {
                return i;
            }
        }
        return null;
    }

    function postChannel(payload) {
        return fetch('/api/channels', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify(payload)
        }).then(function (response) {
            return response.json().then(function (result) {
                return { ok: response.ok, status: response.status, result: result };
            });
        });
    }

    /**
     * Poll GET /api/channel-operations/:id until completed, failed, or timeouts.
     * Behavior aligned with legacy radio.html pollOperationStatus.
     *
     * @param {number} operationId
     * @param {object} options
     * @param {HTMLElement|null} [options.button]
     * @param {string} [options.originalButtonText]
     * @param {function(string): void} [options.setButtonHtml]
     * @param {function(string): void} [options.onFailed]
     * @param {function(): void} [options.onSlowOperationWarning]
     * @param {function(): Promise<void>} [options.onFinalTimeout]
     * @param {number} [options.maxWaitSeconds=60]
     * @param {number} [options.extendedMaxWaitSeconds=120]
     * @param {number} [options.checkIntervalMs=1000]
     * @returns {Promise<'completed'|'failed'|'timeout'>}
     */
    function pollChannelOperation(operationId, options) {
        options = options || {};
        var maxWait = options.maxWaitSeconds !== undefined ? options.maxWaitSeconds : 60;
        var extendedMaxWait = options.extendedMaxWaitSeconds !== undefined ? options.extendedMaxWaitSeconds : 120;
        var checkInterval = options.checkIntervalMs !== undefined ? options.checkIntervalMs : 1000;
        var button = options.button || null;
        var originalButtonText = options.originalButtonText || '';
        var setButtonHtml = options.setButtonHtml;

        function setBtn(html) {
            if (typeof setButtonHtml === 'function') {
                setButtonHtml(html);
            } else if (button) {
                button.innerHTML = html;
            }
        }

        function fetchStatus() {
            return fetch('/api/channel-operations/' + operationId).then(function (r) {
                return r.json();
            });
        }

        function resetButton() {
            if (button) {
                button.disabled = false;
                button.innerHTML = originalButtonText;
            }
        }

        return (async function pollAsync() {
            var startTime = Date.now();
            var attempts = 0;
            var maxAttempts = Math.floor(maxWait * 1000 / checkInterval);

            while (attempts < maxAttempts) {
                await new Promise(function (resolve) {
                    setTimeout(resolve, checkInterval);
                });
                attempts++;
                try {
                    var result = await fetchStatus();
                    if (result.status === 'completed') {
                        return 'completed';
                    }
                    if (result.status === 'failed') {
                        if (typeof options.onFailed === 'function') {
                            options.onFailed(result.error_message || 'Channel operation failed');
                        }
                        resetButton();
                        return 'failed';
                    }
                    var elapsed = Math.floor((Date.now() - startTime) / 1000);
                    setBtn(
                        '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Processing... (' +
                            elapsed +
                            's)'
                    );
                } catch (error) {
                    console.error('Error polling operation status:', error);
                }
            }

            await new Promise(function (resolve) {
                setTimeout(resolve, checkInterval);
            });

            try {
                var result2 = await fetchStatus();
                if (result2.status === 'completed') {
                    return 'completed';
                }
                if (result2.status === 'failed') {
                    if (typeof options.onFailed === 'function') {
                        options.onFailed(result2.error_message || 'Channel operation failed');
                    }
                    resetButton();
                    return 'failed';
                }
            } catch (error) {
                console.error('Error checking final status:', error);
            }

            if (typeof options.onSlowOperationWarning === 'function') {
                options.onSlowOperationWarning();
            }
            setBtn('<span class="spinner-border spinner-border-sm me-2" role="status"></span>Still processing...');

            var extendedMaxAttempts = Math.floor(extendedMaxWait * 1000 / checkInterval);
            while (attempts < extendedMaxAttempts) {
                await new Promise(function (resolve) {
                    setTimeout(resolve, checkInterval);
                });
                attempts++;
                try {
                    var result3 = await fetchStatus();
                    if (result3.status === 'completed') {
                        return 'completed';
                    }
                    if (result3.status === 'failed') {
                        if (typeof options.onFailed === 'function') {
                            options.onFailed(result3.error_message || 'Channel operation failed');
                        }
                        resetButton();
                        return 'failed';
                    }
                    var elapsed2 = Math.floor((Date.now() - startTime) / 1000);
                    setBtn(
                        '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Still processing... (' +
                            elapsed2 +
                            's)'
                    );
                } catch (error) {
                    console.error('Error polling operation status:', error);
                }
            }

            if (typeof options.onFinalTimeout === 'function') {
                await options.onFinalTimeout();
            }
            resetButton();
            return 'timeout';
        })();
    }

    global.MeshCoreChannelOps = {
        getLowestAvailableChannelIndex: getLowestAvailableChannelIndex,
        postChannel: postChannel,
        pollChannelOperation: pollChannelOperation
    };
})(typeof window !== 'undefined' ? window : this);
