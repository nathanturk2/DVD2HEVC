// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

import java.awt.*;
import java.awt.event.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import javax.tv.xlet.*;
import javax.media.*;
import javax.tv.locator.Locator;
import javax.tv.service.selection.*;
import org.bluray.net.BDLocator;
import org.bluray.media.PrimaryAudioControl;
import org.bluray.media.SubtitlingControl;
import org.bluray.system.RegisterAccess;
import org.dvb.event.*;
import org.dvb.ui.DVBBufferedImage;
import org.havi.ui.*;

/** Original DVD video plus timed SPU/highlight graphics, using public BD-J APIs. */
public final class DvdXlet extends HComponent implements Xlet, UserEventListener, ControllerListener, Runnable, MouseListener, MouseMotionListener, ServiceContextListener {
    private XletContext context;
    private HScene scene;
    private Navigator nav;
    private Player player;
    private Thread worker;
    private volatile boolean alive,ended;
    private boolean lifecyclePaused,pausedStarted;
    private final PlaybackClock clock=new PlaybackClock();
    private volatile String failure;
    private long serial=-1,stillUntil=-1,actionUntil=0,endWall=0,endMedia=0;
    private DiscModel.Graphics graphics;
    private final SubpictureTimeline subtitles=new SubpictureTimeline();
    private DiscModel.Menu menu;
    private DiscModel.Menu paintedMenu;
    private int selected=0,lastAudio=-1;
    private int pointerX=Integer.MIN_VALUE,pointerY=Integer.MIN_VALUE;
    private long forcedActivated=Long.MIN_VALUE;
    private int loadedPlaylist=-1,loadedCell=0;
    private int lastNativeAudio=-1;
    private int lastDvdSubtitle=-1,lastNativeSubtitle=-1;
    private boolean lastNativeSubtitleOn;
    private long previousPosition=-1;
    private long loadedStart,loadedDuration;
    private String loadedPgc="";
    private final Map<String,DVBBufferedImage> images=new LinkedHashMap<String,DVBBufferedImage>(16,0.75f,true);
    private int cachedPixels=0;
    private DiscModel.Picture lastPicture;
    private DiscModel.Picture repaintPicture;
    private DiscModel.Menu repaintMenu;
    private int repaintSelection=-1,repaintWidth=-1,repaintHeight=-1;
    private boolean repaintActive;
    private int lastSub=-1;
    private String debugPgc;
    private long debugSeek=-1;
    private int debugSubtitle=-1;
    private int requestedService=-1;
    private volatile int activeService=-1;
    private volatile String serviceFailure;
    private volatile boolean servicePending;
    private final AtomicInteger externalService=new AtomicInteger(-1);
    private long serviceDeadline;
    private ServiceContext titleContext;

    public void initXlet(XletContext context) throws XletStateChangeException {
        this.context=context;
        try {
            nav=new Navigator(new DiscModel(getClass().getResourceAsStream("/disc.bin")),new DvdVm(new Random(),clock));
            scene=HSceneFactory.getInstance().getDefaultHScene();
            scene.setLayout(null);setBounds(0,0,scene.getWidth(),scene.getHeight());scene.add(this);
            addMouseListener(this);
            addMouseMotionListener(this);
            UserEventRepository keys=new UserEventRepository("DVD navigation");
            keys.addAllArrowKeys();keys.addKey(KeyEvent.VK_ENTER);
            // Back = DVD root menu, page up/down = chapters; red/green = audio/subtitle.
            for(int k:new int[]{461,403,404,405,406,KeyEvent.VK_PAGE_UP,KeyEvent.VK_PAGE_DOWN}) keys.addKey(k);
            EventManager.getInstance().addUserEventListener(this,keys);
            titleContext=ServiceContextFactory.getInstance().getServiceContext(context);
            titleContext.addListener(this);
        } catch(Exception e) {throw new XletStateChangeException(e.toString());}
    }
    public synchronized void startXlet() {
        setBounds(0,0,scene.getWidth(),scene.getHeight());
        scene.setVisible(true);setVisible(true);requestFocus();
        if(lifecyclePaused) {
            lifecyclePaused=false;clock.pause(false);
            if(pausedStarted && player!=null && !ended)player.start();
        }
        if(worker==null) {alive=true;worker=new Thread(this,"DVD-VM");worker.start();}
    }
    public synchronized void pauseXlet() {
        lifecyclePaused=true;clock.pause(true);
        pausedStarted=player!=null && player.getState()==Controller.Started;
        if(pausedStarted)player.stop();
    }
    public synchronized void destroyXlet(boolean unconditional) {
        alive=false;
        EventManager.getInstance().removeUserEventListener(this);
        if(titleContext!=null)titleContext.removeListener(this);
        if(player!=null)player.close();
        if(scene!=null) {scene.remove(this);HSceneFactory.getInstance().dispose(scene);}
    }
    private void log(String s) {System.out.println("DVD2UHD "+s);}
    private void preferences() {
        try {
            RegisterAccess settings=RegisterAccess.getInstance();
            nav.vm.sprm[0]=language(settings.getPSR(RegisterAccess.PSR_MENU_DESCR_LANG_CODE),nav.vm.sprm[0]);
            nav.vm.sprm[16]=language(settings.getPSR(RegisterAccess.PSR_LANG_CODE_AUDIO),nav.vm.sprm[16]);
            nav.vm.sprm[18]=language(settings.getPSR(RegisterAccess.PSR_LANG_CODE_PG_TXTST),nav.vm.sprm[18]);
            int country=settings.getPSR(RegisterAccess.PSR_COUNTRY_CODE);
            if((country>>8)>='A' && (country>>8)<='Z' && (country&255)>='A' && (country&255)<='Z')nav.vm.sprm[12]=country;
            log("PREFERENCES menu="+nav.vm.sprm[0]+" audio="+nav.vm.sprm[16]+" subtitle="+nav.vm.sprm[18]);
        } catch(RuntimeException e){log("PREFERENCES defaults: "+e.toString());}
    }
    private int language(int code,int fallback) {
        String iso=""+(char)((code>>16)&255)+(char)((code>>8)&255)+(char)(code&255);
        // Locale providers can take longer than libbluray's Xlet init deadline
        // to enumerate. The ISO language table needs no provider enumeration.
        for(String two:Locale.getISOLanguages())try {
            if(new Locale(two).getISO3Language().equals(iso))return (two.charAt(0)<<8)|two.charAt(1);
        } catch(MissingResourceException e){}
        return fallback;
    }
    private void fail(Throwable e) {
        failure=e.toString();log("ERROR "+failure);e.printStackTrace();
        if(player!=null)player.stop();repaint();
    }
    private long time() {
        if(player==null)return 0;
        DiscModel.Cell c=nav.currentCell();
        long t=Math.max(0,position()-(c==null?0:c.start));
        // DVD still video can contain one coded frame with a longer IFO presentation.
        if(ended && endWall>=0)t=Math.max(t,endMedia+clock.millis()-endWall);
        long duration=c==null?0:(c.end>c.start?c.end-c.start:c.presentation);
        return duration>0?Math.min(t,Math.max(duration,endMedia)):t;
    }
    // libbluray's JMF getMediaTime() advances even when its rate is zero;
    // getMediaNanoseconds() observes pause. Both are public JMF methods.
    private long position() {return player==null?0:player.getMediaNanoseconds()/1000000;}
    private boolean timedPresentation(DiscModel.Cell c) {
        // Ordinary video finishes through EndOfMediaEvent, after VLC drains
        // its decoders. Its JMF clock starts before buffered A/V is presented;
        // stopping it at the IFO duration can discard seconds of queued media.
        // Repeated or natively held pictures instead need the DVD still clock.
        return c.repeated || c.still!=0 || (nav.cell==nav.pgc.cells.length && nav.pgc.still!=0);
    }
    private boolean continuesPlaylist(DiscModel.Cell c) {
        // A joined run may end before the PGC (for a command or still). Wait
        // for native EOF there; only adjacent items in this same playlist can
        // advance without closing the decoder and losing its buffered tail.
        return c.end>c.start && nav.cell<nav.pgc.cells.length &&
               nav.pgc.cells[nav.cell].playlist==c.playlist;
    }
    private boolean advancePresentation(DiscModel.Cell c,long absolute,long t,long now) {
        if(ended || clock.paused())return false;
        if(continuesPlaylist(c) && absolute>=c.end) {
            nav.endCell();return true;
        }
        if(c.end==0 && timedPresentation(c) && c.presentation>0 && t>=c.presentation)
            presentationEnded(c,now);
        return false;
    }
    private void presentationEnded(DiscModel.Cell c,long now) {
        endMedia=c.presentation;endWall=now;ended=true;
        // Native still mode keeps the final picture and live overlay output.
        // stop() closes that output, freezing highlights despite working clicks.
        boolean dvdStill=c.still!=0 || (nav.cell==nav.pgc.cells.length && nav.pgc.still!=0);
        if(!c.repeated && !dvdStill)player.stop();
    }
    public void receiveServiceContextEvent(ServiceContextEvent event) {
        if(event.getServiceContext()!=titleContext)return;
        if(event instanceof NormalContentEvent) {
            try {
                Locator locator=titleContext.getService().getLocator();
                int service=new BDLocator(locator.toExternalForm()).getTitleNumber();
                if(servicePending && service==requestedService)activeService=service;
                else if(service==0 || service==1)externalService.set(service);
            } catch(Exception error) {serviceFailure=error.toString();}
        } else if(event instanceof SelectionFailedEvent && servicePending)serviceFailure="BD-J service selection failed";
    }
    private void externalService() {
        int service=externalService.getAndSet(-1);
        if(service<0)return;
        activeService=requestedService=service;servicePending=false;serviceFailure=null;
        if(nav==null || nav.pgc==null)return;
        // VLC's Disc Menu button selects Top Menu directly, without sending a
        // popup key. The persistent Xlet must also enter the DVD's menu domain.
        if(service==0 && nav.pgc.title) {
            int mask=nav.pgc.uops | (graphics==null?0:graphics.uops(nav.currentMillis));
            if((mask&(1<<11))!=0 || !nav.rootMenu(nav.currentMillis)) {
                // A forbidden or absent DVD menu must leave the movie at its
                // current cell/offset, even though VLC already selected title 0.
                nav.resumeMillis=nav.currentMillis;nav.serial++;
                log("TOP MENU unavailable");return;
            }
            log("TOP MENU "+nav.pgc.key);
        } else if(service==1 && !nav.pgc.title) {
            int mask=nav.pgc.uops | (graphics==null?0:graphics.uops(nav.currentMillis));
            if((mask&(1<<16))!=0) {
                nav.resumeMillis=nav.currentMillis;nav.serial++;
                log("TITLE resume unavailable");return;
            }
            if(!nav.resumeTitle())nav.selectTitle(1);
            log("TITLE "+nav.pgc.key);
        }
    }
    private boolean selectService(int service) throws Exception {
        if(servicePending) {
            if(serviceFailure!=null)throw new IOException(serviceFailure);
            if(activeService!=requestedService) {
                if(System.nanoTime()>=serviceDeadline)throw new IOException("BD-J service selection timed out");
                return false;
            }
            servicePending=false;log("SERVICE ready="+activeService);
        }
        if(requestedService==service && activeService==service)return true;
        if(player!=null) {player.removeControllerListener(this);player.close();player=null;loadedPlaylist=-1;}
        menu=null;clearImages();
        activeService=-1;serviceFailure=null;requestedService=service;servicePending=true;
        serviceDeadline=System.nanoTime()+10000000000L;
        titleContext.select(new Locator[]{new BDLocator(null,service,-1)});
        log("SERVICE request="+service);
        return false;
    }
    private boolean play() throws Exception {
        DiscModel.Cell c=nav.currentCell();
        // Interactive menu services make stock VLC deliberately suppress time,
        // duration and position. A persistent Xlet selects the movie service
        // for DVD title domains and top-menu service for DVD menu domains.
        // Both reference the same BDJO/app, so VM registers/resume survive.
        int service=nav.pgc.title?1:0;
        // Selection completes on libbluray's loader thread, which can call
        // startXlet(). Yield our monitor until its NormalContentEvent arrives;
        // otherwise the old title's timeline can overwrite the new playlist.
        if(!selectService(service))return false;
        boolean same=reusablePlayer(c);
        boolean continuation=same && nav.continuation && loadedCell+1==nav.cell && position()>=c.start-100;
        boolean consecutive=c!=null && nav.continuation && loadedPgc.equals(nav.pgc.key) && loadedCell+1==nav.cell;
        long boundary=consecutive?(same?c.start-loadedStart:loadedDuration):0;
        if(!same && player!=null) {player.removeControllerListener(this);player.close();player=null;}
        menu=null;clearImages();selected=nav.vm.sprm[8]>>10;pointerX=pointerY=Integer.MIN_VALUE;forcedActivated=Long.MIN_VALUE;ended=false;endWall=-1;endMedia=0;stillUntil=-1;lastAudio=-1;lastNativeAudio=-1;lastDvdSubtitle=-1;previousPosition=-1;
        if(c==null) {graphics=null;log("STOP");return true;}
        if(c.playlist<0) throw new IOException("DVD cell was not authored: "+nav.pgc.key+":"+nav.cell);
        graphics=new DiscModel.Graphics(getClass().getResourceAsStream("/"+c.graphics));
        subtitles.enter(graphics,boundary,consecutive);
        loadedStart=c.start;loadedDuration=c.end>c.start?c.end-c.start:c.duration;
        if(same) {
            if(!continuation && !nav.mediaSynchronized)player.setMediaTime(new Time((c.start+nav.resumeMillis)/1000.0));
            nav.resumeMillis=0;loadedCell=nav.cell;
            log((nav.mediaSynchronized?"SYNC ":continuation?"CONTINUE ":"SEEK ")+nav.pgc.key+" cell="+nav.cell+" playlist="+c.playlist);
            if(!nav.mediaSynchronized && !continuation && !lifecyclePaused)player.start();
            audio();subtitleControl();repaint();return true;
        }
        BDLocator locator=new BDLocator(null,1,c.playlist);
        log("PLAY "+nav.pgc.key+" cell="+nav.cell+" playlist="+c.playlist);
        player=Manager.createPlayer(new MediaLocator(locator.toExternalForm()));
        player.realize();
        long deadline=System.currentTimeMillis()+10000;
        while(player.getState()<Controller.Realized && System.currentTimeMillis()<deadline)Thread.sleep(20);
        if(player.getState()<Controller.Realized)throw new IOException("BD-J player realization timed out");
        player.addControllerListener(this);
        if(c.start+nav.resumeMillis>0) {player.setMediaTime(new Time((c.start+nav.resumeMillis)/1000.0));nav.resumeMillis=0;}
        loadedPlaylist=c.playlist;loadedPgc=nav.pgc.key;loadedCell=nav.cell;
        audio();subtitleControl();
        if(!lifecyclePaused)player.start();
        repaint();return true;
    }
    private boolean reusablePlayer(DiscModel.Cell c) {
        // VLC has drained and torn down an EOF player's native input. A DVD
        // cell command can loop back to the very same playlist; reopen it
        // rather than seeking an exhausted player into a blank video output.
        return c!=null && player!=null && loadedPlaylist==c.playlist && loadedPgc.equals(nav.pgc.key) &&
               !(ended && player.getState()!=Controller.Started);
    }
    private void audio() throws Exception {
        if(player==null || nav.pgc==null) return;
        // A joined DVD run can finish with a silent black/control cell. The
        // native player may still enumerate the preceding play item's audio;
        // that does not create a physical DVD stream in this cell.
        if(nav.currentCell()==null || nav.currentCell().audioStreams.length==0)return;
        int logical=nav.vm.sprm[1]&7, control=nav.pgc.audio[logical];
        if((control&0x8000)==0) {
            for(int i=0;i<8;i++)if((nav.pgc.audio[i]&0x8000)!=0) {logical=i;control=nav.pgc.audio[i];break;}
        }
        int physical=(control>>8)&7;
        if((control&0x8000)==0 && !nav.pgc.title)physical=0;
        PrimaryAudioControl ac=(PrimaryAudioControl)player.getControl("org.bluray.media.PrimaryAudioControl");
        if(ac!=null && ac.listAvailableStreamNumbers().length>0) {
            int[] available=ac.listAvailableStreamNumbers();
            int position=-1;int[] map=nav.currentCell().audioStreams;
            int current=ac.getCurrentStreamNumber();
            if(lastAudio==physical && lastNativeAudio>=0) {
                if(current==lastNativeAudio)return;
                boolean blocked=((nav.pgc.uops|graphics.uops(time()))&(1<<20))!=0;
                for(int i=0;!blocked && i<available.length && i<map.length;i++)if(available[i]==current) {
                    for(int n=0;n<8;n++)if((nav.pgc.audio[n]&0x8000)!=0 && ((nav.pgc.audio[n]>>8)&7)==map[i]) {
                        nav.vm.sprm[1]=n;lastAudio=map[i];lastNativeAudio=current;
                        log("AUDIO native logical="+n+" physical="+map[i]);return;
                    }
                }
            }
            for(int i=0;i<map.length;i++)if(map[i]==physical)position=i;
            if(position<0 || position>=available.length)throw new IOException("DVD audio mapping absent in BD playlist");
            ac.selectStreamNumber(available[position]);lastAudio=physical;lastNativeAudio=available[position];
            log("AUDIO logical="+logical+" physical="+physical);
        }
    }
    private void subtitleControl() throws Exception {
        DiscModel.Cell cell=nav.currentCell();
        if(player==null || cell==null || !cell.nativeSubtitles || !nav.pgc.title)return;
        SubtitlingControl sc=(SubtitlingControl)player.getControl("org.bluray.media.SubtitlingControl");
        if(sc==null)return;
        int[] available=sc.listAvailableStreamNumbers();
        if(available.length==0)return;
        int current=sc.getCurrentStreamNumber();boolean on=sc.isSubtitlingOn();
        int dvd=nav.vm.sprm[2];
        if(lastDvdSubtitle==dvd && (current!=lastNativeSubtitle || on!=lastNativeSubtitleOn)) {
            boolean blocked=((nav.pgc.uops|graphics.uops(time()))&(1<<21))!=0;
            int logical=-1;
            for(int n=0;n<available.length;n++)if(available[n]==current)logical=n;
            boolean usable=logical>=0 && logical<nav.pgc.sub.length && (nav.pgc.sub[logical]&0x80000000)!=0;
            if(!blocked && (!on || usable)) {
                // Disabling preserves the DVD logical slot, including forced captions.
                nav.vm.sprm[2]=on?(logical|64):(dvd&31);dvd=nav.vm.sprm[2];
                log("SUBTITLE native logical="+(dvd&31)+" enabled="+on);
            }
        }
        if(lastDvdSubtitle!=dvd || current!=lastNativeSubtitle || on!=lastNativeSubtitleOn) {
            int logical=dvd&31;
            // Disabled DVD defaults can name slot 30. Publish a usable native
            // selection without rewriting that original register value.
            int stream=available[logical<available.length?logical:0];
            if(current!=stream)sc.selectStreamNumber(stream);
            sc.setSubtitling((dvd&64)!=0);
            lastDvdSubtitle=dvd;
            // Read back the public control rather than assuming setter timing.
            lastNativeSubtitle=sc.getCurrentStreamNumber();lastNativeSubtitleOn=sc.isSubtitlingOn();
        }
    }
    public void run() {
        try {
            synchronized(this) {
                preferences();
                nav.start();
                String[] args=(String[])context.getXletProperty(XletContext.ARGS);
                if(args!=null)for(String arg:args) {
                    if(arg.startsWith("debug-root="))nav.debugRoot(Integer.parseInt(arg.substring(11)));
                    if(arg.startsWith("debug-title=")) {
                        int requested=Integer.parseInt(arg.substring(12));nav.selectTitle(requested);
                        for(DiscModel.Title title:nav.disc.titles)if(title.number==requested)debugPgc="T:"+title.vts+":"+title.pgc[0];
                    }
                    if(arg.startsWith("debug-seek-ms="))debugSeek=Long.parseLong(arg.substring(14));
                    if(arg.startsWith("debug-subtitle="))debugSubtitle=Integer.parseInt(arg.substring(15));
                }
            }
            while(alive) {
                synchronized(this) {
                    if(failure==null && !lifecyclePaused) {
                        externalService();
                        nav.tick();
                        if(nav.serial!=serial) {
                            // A DVD title's pre-commands may divert through mandatory
                            // intros/menus. Apply test position/subtitle only when
                            // the requested PGC is actually entered, preserving them.
                            if(debugPgc==null || debugPgc.equals(nav.pgc.key)) {
                                if(debugSeek>=0) {nav.resumeMillis=debugSeek;debugSeek=-1;}
                                if(debugSubtitle>=0) {nav.vm.sprm[2]=debugSubtitle;debugSubtitle=-1;}
                            }
                            if(play())serial=nav.serial;
                        }
                        if(nav.serial==serial && !nav.stopped) {
                            long absolute=position();
                            DiscModel.Cell present=nav.currentCell();
                            boolean seek=previousPosition>=0 && Math.abs(absolute-previousPosition)>1500;
                            if(seek){log("POSITION discontinuity="+absolute);subtitles.discardCarry();}
                            // Ordinary adjacent playback runs the original cell-end path.
                            // Backward seeks and jumps over cells adopt the native position.
                            if(present.end>present.start && (seek || absolute<present.start ||
                                (nav.cell<nav.pgc.cells.length && absolute>=nav.pgc.cells[nav.cell].end))) {
                                if(nav.synchronizeMedia(loadedPlaylist,absolute))continue;
                            }
                            long t=time(),now=clock.millis();
                            previousPosition=absolute;
                            nav.currentMillis=t;
                            updateMenu(t);audio();subtitleControl();
                            DiscModel.Cell c=nav.currentCell();
                            // The asset can contain repeated still pictures for
                            // decoder startup. DVD timing remains the IFO timing.
                            if(advancePresentation(c,absolute,t,now))continue;
                            if(ended && stillUntil<0) {
                                int still=c.still!=0?c.still:(nav.cell==nav.pgc.cells.length?nav.pgc.still:0);
                                stillUntil=still==255?Long.MAX_VALUE:now+Math.max(0,c.presentation-t)+still*1000L;
                                log("END cell="+nav.cell+" still="+still);
                            }
                            // MPLS native still mode retains this decoder and overlay.
                            // Repeated seeks through a 500 ms tail rebuffered VLC and
                            // could blank/resize the video while navigating menus.
                            if(!clock.paused() && stillUntil>=0 && now>=stillUntil) nav.endCell();
                            if(nav.serial==serial)repaintIfChanged(t);
                        }
                    }
                }
                Thread.sleep(40);
            }
        } catch(Throwable e) {fail(e);}
    }
    private void updateMenu(long t) {
        DiscModel.Menu next=null;
        if(graphics!=null) for(DiscModel.Menu m:graphics.menus) if(t>=m.start && t<m.end)next=m;
        if(menu!=next) {
            DiscModel.Menu previous=menu;
            menu=next;
            if(menu!=null) {
                // Repeated PCI snapshots of the same HLI must not undo a
                // user's selection in a motion menu. A new HLI may force it.
                boolean newHighlight=previous==null || previous.start!=menu.start || previous.select!=menu.select;
                if(menu.select>0 && newHighlight) selected=menu.select;
                else if(selected<1 || selected>menu.count)selected=1;
                nav.vm.sprm[8]=selected<<10;
                log("MENU buttons="+menu.count+" selected="+selected);
                long activation=(menu.start<<6)^menu.activate;
                if(menu.activate>0 && forcedActivated!=activation) {
                    forcedActivated=activation;selected=menu.activate;activate();
                }
            }
        }
        int vmSelected=nav.vm.sprm[8]>>10;
        if(menu!=null && vmSelected>0 && vmSelected<=menu.count)selected=vmSelected;
    }
    public synchronized void controllerUpdate(ControllerEvent e) {
        if(e.getSource()!=player) return;
        if(e instanceof EndOfMediaEvent) {
            DiscModel.Cell c=nav.currentCell();
            if(c!=null && !ended) {
                endMedia=Math.max(0,position()-c.start);
                endWall=clock.millis();ended=true;
            }
        }
        else if(e instanceof RateChangeEvent) {
            clock.pause(lifecyclePaused || ((RateChangeEvent)e).getRate()==0);
            log("RATE "+((RateChangeEvent)e).getRate());
        }
        else if(e instanceof ControllerErrorEvent) fail(new IOException(e.toString()));
    }
    private void select(int n) {
        if(menu==null || n<1 || n>menu.count)return;
        if(selected!=n)log("SELECT button="+n);
        selected=n;nav.vm.sprm[8]=n<<10;
        repaint();
    }
    private void navigate(int n) {
        if(menu==null || n<1 || n>menu.count)return;
        select(n);
        DiscModel.Button b=menu.button(n,nav.pgc.wide);
        // libdvdnav applies auto_action_mode to directional selection only.
        if(b!=null && b.auto!=0)activate();
    }
    private void activate() {
        if(menu==null)return;
        DiscModel.Button b=menu.button(selected,nav.pgc.wide);
        if(b==null)return;
        actionUntil=System.currentTimeMillis()+150;
        log("BUTTON "+selected+" command="+Long.toHexString(b.command));
        nav.currentMillis=time();
        nav.button(b.command,selected);
    }
    public synchronized void userEventReceived(UserEvent e) {
        if(e.getType()!=KeyEvent.KEY_PRESSED || failure!=null || servicePending || nav.serial!=serial)return;
        try {
            int k=e.getCode();
            log("KEY "+k);
            int mask=nav.pgc.uops | (graphics==null?0:graphics.uops(time()));
            int bit=k==461?(nav.pgc.title?11:16):k==KeyEvent.VK_PAGE_UP?6:k==KeyEvent.VK_PAGE_DOWN?7:k==403?20:k==404?21:
                menu!=null?17:ended && k==KeyEvent.VK_ENTER?18:-1;
            if(bit>=0 && (mask&(1<<bit))!=0){log("UOP blocked key="+k);return;}
            if(menu!=null && k!=461 && k!=403 && k!=404 && time()>=menu.selectEnd)return;
            if(k==461) {
                if(nav.pgc.title)nav.rootMenu(time());
                else if(nav.resumeTitle())log("RESUME "+nav.pgc.key+" cell="+nav.cell+" offset="+nav.resumeMillis);
            }
            else if(k==KeyEvent.VK_PAGE_UP)nav.chapter(-1);
            else if(k==KeyEvent.VK_PAGE_DOWN)nav.chapter(1);
            else if(k==403) {
                int n=(nav.vm.sprm[1]+1)&7;
                for(int i=0;i<8;i++,n=(n+1)&7)if((nav.pgc.audio[n]&0x8000)!=0) {nav.vm.sprm[1]=n;break;}
            } else if(k==404) {
                int old=nav.vm.sprm[2],n=(old&31)+1;
                if((old&64)==0)n=0;
                while(n<32 && (nav.pgc.sub[n]&0x80000000)==0)n++;
                nav.vm.sprm[2]=n<32?n|64:old&31;
            } else if(k==KeyEvent.VK_ENTER) {if(menu!=null)activate();else if(ended)nav.endCell();}
            else if(menu!=null) {
                DiscModel.Button b=menu.button(selected,nav.pgc.wide);
                if(b!=null) {
                    if(k==KeyEvent.VK_UP)navigate(b.up);
                    if(k==KeyEvent.VK_DOWN)navigate(b.down);
                    if(k==KeyEvent.VK_LEFT)navigate(b.left);
                    if(k==KeyEvent.VK_RIGHT)navigate(b.right);
                }
            }
        } catch(Throwable ex) {fail(ex);}
    }
    private int argb(int value,int alpha) {
        if(alpha==0)return 0;
        int y=((value>>16)&255)-16,cr=((value>>8)&255)-128,cb=(value&255)-128;
        int r=clamp((298*y+409*cr+128)>>8),g=clamp((298*y-100*cb-208*cr+128)>>8),b=clamp((298*y+516*cb+128)>>8);
        return (alpha*17)<<24 | r<<16 | g<<8 | b;
    }
    private int clamp(int n) {return Math.max(0,Math.min(255,n));}
    private void clearImages() {
        for(DVBBufferedImage img:images.values())img.dispose();
        images.clear();cachedPixels=0;
    }
    private DVBBufferedImage image(DiscModel.Picture p,DiscModel.Button button,int word) throws IOException {
        String key=System.identityHashCode(p)+":"+(button==null?0:selected)+":"+word;
        DVBBufferedImage img=images.get(key);
        if(img!=null)return img;
        // Nine full-raster language highlights fit within a 16 MiB pixel
        // budget. Evict just the least recently used image, not the whole menu.
        while(cachedPixels+p.w*p.h>4*1024*1024 && !images.isEmpty()) {
            Iterator<DVBBufferedImage> oldest=images.values().iterator();
            DVBBufferedImage expired=oldest.next();oldest.remove();
            cachedPixels-=expired.getWidth()*expired.getHeight();expired.dispose();
        }
        img=new DVBBufferedImage(p.w,p.h);
        img.setRGB(0,0,p.w,p.h,picturePixels(p,button,word),0,p.w);
        cachedPixels+=p.w*p.h;images.put(key,img);return img;
    }
    private int[] picturePixels(DiscModel.Picture p,DiscModel.Button button,int word) throws IOException {
        byte[] mask=p.mask();
        int[] pixels=new int[p.w*p.h];
        for(int y=0;y<p.h;y++)for(int x=0;x<p.w;x++) {
            int index=mask[y*p.w+x]&3,col=p.colours[index],alpha=p.alpha[index];
            long changed=p.colourWord(x+p.x,y+p.y);
            if(changed!=-1) {col=(int)(changed >>> (16+index*4))&15;alpha=(int)(changed >>> (index*4))&15;}
            if(button!=null && x+p.x>=button.x && x+p.x<=button.x2 && y+p.y>=button.y && y+p.y<=button.y2) {
                col=(word >>> (16+index*4))&15;alpha=(word >>> (index*4))&15;
            }
            pixels[y*p.w+x]=argb(nav.pgc.palette[col],alpha);
        }
        return pixels;
    }
    private DiscModel.Picture visiblePicture(long t) {
        if(graphics==null || nav.pgc==null)return null;
        int logical=nav.vm.sprm[2]&31,control=nav.pgc.sub[logical];
        if((control&0x80000000)==0)for(int i=0;i<32;i++)if((nav.pgc.sub[i]&0x80000000)!=0) {control=nav.pgc.sub[i];break;}
        int physical=(control >>> (nav.pgc.wide?16:24))&31;
        SubpictureTimeline.Active active=subtitles.active(physical,t);
        if(active==null || (nav.pgc.title && (nav.vm.sprm[2]&64)==0 && !active.picture.forced))return null;
        return active.picture;
    }
    private boolean repaintIfChanged(long t) {
        DiscModel.Picture picture=visiblePicture(t);
        boolean active=System.currentTimeMillis()<actionUntil;
        int width=getWidth(),height=getHeight();
        if(picture==repaintPicture && menu==repaintMenu && selected==repaintSelection &&
           active==repaintActive && width==repaintWidth && height==repaintHeight)return false;
        repaintPicture=picture;repaintMenu=menu;repaintSelection=selected;
        repaintActive=active;repaintWidth=width;repaintHeight=height;
        repaint();return true;
    }
    public synchronized void paint(Graphics g) {
        Graphics2D g2=(Graphics2D)g;
        g2.setComposite(AlphaComposite.Src);g2.setColor(new Color(0,0,0,0));g2.fillRect(0,0,getWidth(),getHeight());
        g2.setComposite(AlphaComposite.SrcOver);
        if(failure!=null) {g2.setColor(Color.RED);g2.drawString("DVD2UHD: "+failure,40,80);return;}
        if(graphics==null || nav.pgc==null)return;
        if(menu!=null && paintedMenu!=menu) {
            paintedMenu=menu;log("PAINT pictures="+graphics.pictures.length+" size="+getWidth()+"x"+getHeight()+" visible="+isVisible());
        }
        long t=time();int logical=nav.vm.sprm[2]&31;
        DiscModel.Picture p=visiblePicture(t);
        if(nav.pgc.title && lastSub!=nav.vm.sprm[2]) {
            lastSub=nav.vm.sprm[2];log("SUBTITLE logical="+logical+" physical="+(p==null?-1:p.stream)+" enabled="+((lastSub&64)!=0));
        }
        DiscModel.Button b=menu==null?null:menu.button(selected,nav.pgc.wide);
        int word=0;
        if(b!=null && b.colour>0) word=menu.colours[(b.colour-1)*2+(System.currentTimeMillis()<actionUntil?1:0)];
        double sx=getWidth()/(double)nav.pgc.width,sy=getHeight()/(double)nav.pgc.height;
        // VLC scales the overlay from the top-left using its own square pixels.
        // Apply DVD display aspect once; centering adds a second 4:3 offset.
        sx=sy*nav.pgc.height/nav.pgc.width*(nav.pgc.wide?16.0/9.0:4.0/3.0);
        double left=0;
        if(p!=null) {
            if(nav.pgc.title && lastPicture!=p) {lastPicture=p;log("PICTURE stream="+p.stream+" start="+p.start+" end="+p.end);}
            try {
                DVBBufferedImage img=image(p,b,word);
                g2.drawImage(img,(int)(left+p.x*sx),(int)(p.y*sy),(int)(p.w*sx),(int)(p.h*sy),null);
            } catch(IOException ex) {fail(ex);return;}
        }
    }
    public void update(Graphics g) {paint(g);}
    public synchronized void mousePressed(MouseEvent e) {
        log("MOUSE x="+e.getX()+" y="+e.getY());
        pointer(e,true);
    }
    private void pointer(MouseEvent e,boolean pressed) {
        if(failure!=null || menu==null || servicePending || nav.serial!=serial || time()>=menu.selectEnd || ((nav.pgc.uops|graphics.uops(time()))&(1<<17))!=0)return;
        // Stock VLC passes encoded-video raster coordinates to BD-J mouse
        // events, rather than coordinates in the HD overlay plane.
        // Match VLC's libdvdnav mouse_select/mouse_activate: inclusive bounds,
        // nearest centre when rectangles overlap, first button wins a tie.
        // Hover selects only; clicking selects and executes exactly once,
        // regardless of auto_action_mode. Outside all buttons does nothing.
        int x=e.getX(),y=e.getY(),best=0,distance=0x08000000;
        // A stationary pointer must not undo directional navigation when a
        // video-output refresh delivers another move at the same coordinates.
        if(!pressed && x==pointerX && y==pointerY)return;
        pointerX=x;pointerY=y;
        for(int n=1;n<=menu.count;n++) {
            DiscModel.Button b=menu.button(n,nav.pgc.wide);
            if(b!=null && x>=b.x && x<=b.x2 && y>=b.y && y<=b.y2) {
                int dx=(b.x+b.x2)/2-x,dy=(b.y+b.y2)/2-y,d=dx*dx+dy*dy;
                if(d<distance) {distance=d;best=n;}
            }
        }
        if(best==0)return;
        if(selected!=best)select(best);
        if(pressed)activate();
    }
    public void mouseClicked(MouseEvent e) {}
    public void mouseReleased(MouseEvent e) {}
    public void mouseEntered(MouseEvent e) {}
    public synchronized void mouseExited(MouseEvent e) {pointerX=pointerY=Integer.MIN_VALUE;}
    public synchronized void mouseMoved(MouseEvent e) {pointer(e,false);}
    public synchronized void mouseDragged(MouseEvent e) {pointer(e,false);}
}
