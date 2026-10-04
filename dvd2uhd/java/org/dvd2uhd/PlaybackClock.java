// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

/** Monotonic DVD elapsed time. Paused time never advances stills or counters. */
public final class PlaybackClock implements DvdVm.Clock {
    public interface Source { long nanos(); }
    private final Source source;
    private long anchor,elapsed;
    private boolean paused;
    public PlaybackClock() {this(new Source(){public long nanos(){return System.nanoTime();}});}
    public PlaybackClock(Source source) {this.source=source;anchor=source.nanos();}
    public synchronized long millis() {
        return (elapsed+(paused?0:Math.max(0,source.nanos()-anchor)))/1000000;
    }
    public synchronized void pause(boolean value) {
        if(paused==value)return;
        long now=source.nanos();
        if(value)elapsed+=Math.max(0,now-anchor);
        anchor=now;paused=value;
    }
    public synchronized boolean paused(){return paused;}
}
