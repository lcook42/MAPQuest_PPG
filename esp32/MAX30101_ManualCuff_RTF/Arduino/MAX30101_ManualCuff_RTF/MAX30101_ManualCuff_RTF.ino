/* Pulsatile RTF research recorder, 2026-10-12-r1.
 * Acquisition runs on ESP32 DevKit V1; the SAME Python detector runs over USB or Wi-Fi.
 * No MAP estimate. TFT results require a connected, running detector.
 * MAX30101: SDA D4/GPIO4, SCL D5/GPIO5.
 * ST7735: SCK GPIO18, MOSI GPIO23, CS GPIO27,
 *         DC GPIO26, RST GPIO25. SD card is NOT used; CS GPIO33 held HIGH.
 */
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <lwip/sockets.h>
#include <errno.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>

static const char *VERSION = "2026-10-12-r3-three";
static const char *AP_NAME = "RTF-ESP32";
static const char *AP_PASSWORD = "RTFtest2026"; // Local test network; change here if desired.
static const uint8_t SENSOR = 0x57;
static const uint32_t WARN_MS = 60000, ABORT_MS = 90000;
// Common 1.44-inch 128x128 ST7735 module. Panel variants may need a different tab.
#ifndef TFT_TAB
#define TFT_TAB INITR_144GREENTAB
#endif
Adafruit_ST7735 tft(27, 26, 25);
WiFiServer server(8765);
WiFiClient client;
bool healthy = false, running = false, armed = false, warned = false;
bool usingWifi = false;
bool diagnostic=false;
bool rtfThree=false;
uint8_t greenLed=32;
uint8_t redLed = 32, irLed = 32;
uint32_t seq = 0, epoch = 0, armAt = 0, lastPing = 0, lastSample = 0;
uint32_t lastPoll = 0, lastScreen = 0, lastConfigCheck = 0;
String usbLine, netLine, screenState = "WAITING", screenDetail = "Start laptop app";
String outbound;
uint32_t queuedAt = 0;
Preferences networkPrefs;
bool mdnsReady=false;

bool decodeHex(const String &hex, String &value) {
  value="";
  if (hex.length()%2) return false;
  for (unsigned j=0;j<hex.length();j+=2) {
    char pair[3]={hex[j],hex[j+1],0}; char *end;
    long b=strtol(pair,&end,16);
    if (*end || b<1 || b>255) return false;
    value+=char(b);
  }
  return true;
}

bool regWrite(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(SENSOR); Wire.write(reg); Wire.write(value);
  return Wire.endTransmission() == 0;
}
bool regRead(uint8_t reg, uint8_t *out, uint8_t n) {
  Wire.beginTransmission(SENSOR); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(SENSOR, n) != n) return false;
  for (uint8_t i=0; i<n; ++i) out[i] = Wire.read();
  return true;
}
void emit(const String &line) {
  if (usingWifi) {
    if (!client.connected()) return;
    if (outbound.length()+line.length()+1>4096) {
      running=false; screenState="STOP / RELEASE"; screenDetail="WiFi backlog";
      outbound=""; client.stop(); return;
    }
    if (!outbound.length()) queuedAt=millis();
    outbound+=line; outbound+='\n';
  }
  else if (Serial) Serial.println(line);
}
void pumpNetwork() {
  if (!outbound.length() || !client.connected()) return;
  // Avoid WiFiClient::write's multi-second retries blocking FIFO service/timers.
  int sent=::send(client.fd(),outbound.c_str(),outbound.length(),MSG_DONTWAIT);
  if (sent>0) { outbound.remove(0,sent); if(outbound.length()) queuedAt=millis(); }
  else if ((sent<0 && errno!=EAGAIN && errno!=EWOULDBLOCK) || millis()-queuedAt>500) {
    running=false; screenState="STOP / RELEASE"; screenDetail="WiFi send stalled";
    outbound=""; client.stop();
  }
}
bool flushFifo() {
  // Stop conversions while clearing a FIFO that may have filled before START.
  if (!regWrite(0x09,diagnostic?0x87:0x83)) return false;
  delay(12);
  uint8_t discarded[9], status[2];
  if (!regRead(0x07,discarded,diagnostic?9:6) || !regWrite(0x04,0)
      || !regWrite(0x06,0) || !regWrite(0x05,0)
      || !regRead(0x00,status,2) || !regWrite(0x09,diagnostic?0x07:0x03)) return false;
  ++epoch; lastSample = millis(); lastPoll = millis();
  return true;
}
void fault(const char *reason) {
  running = false;
  // Preserve armed timer: an optical/transport error cannot release a manual cuff.
  screenState = "STOP / RELEASE"; screenDetail = reason;
  emit(String("FAULT,") + reason);
}
void drawScreen() {
  // Short SPI update, bounded to ~20 ms; FIFO holds up to 320 ms at 100 Hz.
  tft.fillScreen(ST77XX_BLACK);
  tft.setTextWrap(true); tft.setTextSize(1); tft.setCursor(3,3);
  tft.setTextColor(ST77XX_CYAN); tft.println("PPG RTF  /  RESEARCH");
  tft.setTextColor(ST77XX_WHITE);
  tft.println(WiFi.status()==WL_CONNECTED ? "WiFi: Home network" : "WiFi: RTF-ESP32");
  tft.print(WiFi.status()==WL_CONNECTED ? WiFi.localIP() : WiFi.softAPIP());
  tft.println(":8765"); tft.println();
  bool alert = screenState.indexOf("STOP") >= 0 || screenState == "RTF";
  tft.setTextColor(alert ? ST77XX_YELLOW : ST77XX_GREEN);
  tft.println(screenState); tft.setTextColor(ST77XX_WHITE);
  tft.println(screenDetail); tft.println();
  if (armed) {
    tft.setTextColor(ST77XX_YELLOW);
    tft.print("Cuff timer: "); tft.print((millis()-armAt)/1000); tft.println("s");
  }
  tft.setTextColor(ST77XX_WHITE);
  tft.println(running ? "Live samples" : "No live detection");
  tft.println("Manual cuff release");
}
void hello() {
  emit(String("HELLO,") + VERSION + ",100,RED_IR,60,90,ESP32_DEVKIT_V1");
}
bool initializeSensor() {
  diagnostic=false;rtfThree=false;redLed=irLed=greenLed=32;
  uint8_t id=0,reset=0x40;
  bool ok=regRead(0xFF,&id,1) && id==0x15 && regWrite(0x09,0x40);
  uint32_t start=millis();
  while(ok && (reset&0x40) && millis()-start<200) {
    ok=regRead(0x09,&reset,1);delay(1);
  }
  ok=ok && !(reset&0x40);
  ok=ok && regWrite(0x02,0) && regWrite(0x03,0) && regWrite(0x08,0x0F)
    && regWrite(0x0A,0x67) && regWrite(0x0C,32) && regWrite(0x0D,32)
    && regWrite(0x0E,0) && regWrite(0x09,0x03);
  return ok && flushFifo();
}
void command(String s, bool wifi) {
  s.trim();
  if (!s.length()) return;
  // One transport owns acquisition. A second client cannot change LEDs or stop a test.
  if (running && wifi != usingWifi) return;
  if (s=="NET") {
    if (!wifi) Serial.println(String("NET,")+(WiFi.status()==WL_CONNECTED ? WiFi.localIP().toString() : "DISCONNECTED"));
    return;
  }
  if (s.startsWith("WIFI,")) {
    // Provision only over physical USB while no measurement is running.
    if (wifi) return;
    if (armed || running) { Serial.println("ERROR,WIFI_BUSY"); return; }
    int comma=s.indexOf(',',5); String ssid,password;
    if (comma<0 || !decodeHex(s.substring(5,comma),ssid) || !decodeHex(s.substring(comma+1),password)
        || ssid.length()<1 || ssid.length()>32 || password.length()<8 || password.length()>63) {
      Serial.println("ERROR,WIFI_FORMAT"); return;
    }
    networkPrefs.putString("ssid",ssid); networkPrefs.putString("password",password);
    WiFi.disconnect(false,false); WiFi.begin(ssid.c_str(),password.c_str());
    password=""; s="";
    screenState="WIFI SETUP"; screenDetail="Joining home WiFi";
    Serial.println("ACK,WIFI"); return;
  }
  if (s == "HELP" || s == "help") {
    if (!running) usingWifi=wifi;
    emit("HELP,MANUAL CUFF / PULSATILE RTF");
    emit("HELP,Keep cuff deflated during setup. No external MAP is required.");
    emit("HELP,Sensor: upper arm or forearm distal to cuff, outside cuff compression. Recalibrate after moving.");
    emit("HELP,Close Serial Monitor then run Start_USB.cmd or Start_WiFi.cmd.");
    emit("HELP,On laptop open http://127.0.0.1:8766");
    emit("HELP,Dashboard: Calibrate > Start cuff phase > Start return phase > Cuff is fully released.");
    emit("HELP,Record pressure is optional. STOP stops software; release cuff manually.");
    emit("HELP,Old calplot/plot/measure modes are retired. HELP ADVANCED lists diagnostics.");
    return;
  }
  if (s == "HELP ADVANCED" || s == "help advanced") {
    if (!running) usingWifi=wifi;
    emit("HELP,ADVANCED / HOST PROTOCOL - normally managed by the laptop app");
    emit("HELP,HELLO: firmware identity. START: raw recording. PING: keepalive. STOP: stop.");
    emit("HELP,LED,r,i: optical setup before cuff phase only. App normally tunes and locks LEDs.");
    emit("HELP,ARM/RELEASED/STATUS are app-managed. Use dashboard for a coordinated trial.");
    emit("HELP,USB 115200; WiFi RTF-ESP32; TCP 192.168.4.1:8765 (not a webpage).");
    emit("HELP,Home WiFi: run Configure_WiFi.cmd over USB. NET shows home IP. TFT shows active IP.");
    emit("HELP,Use one recorder connection. Do not open Serial Monitor while USB dashboard owns port.");
    emit("HELP,No MAP calculation; nonpulsatile measurement is unsupported.");
    return;
  }
  if (s == "HELLO") { usingWifi = wifi; hello(); return; }
  if (s == "PING") { lastPing = millis(); return; }
  if (s == "STOP") { usingWifi = wifi; running = false; screenState="STOP / RELEASE"; screenDetail="Fully release cuff"; emit("ACK,STOP"); return; }
  if (s == "RELEASED") { armed=false; warned=false; emit("ACK,RELEASED"); return; }
  if (s == "START") {
    usingWifi=wifi;
    if (armed) { emit("ERROR,RELEASE_CUFF_FIRST"); return; }
    running=false;healthy=initializeSensor();
    if (!healthy) { emit("FAULT,SENSOR_INIT"); return; }
    diagnostic=false;rtfThree=false;
    if (!regWrite(0x09,0x83)||!regWrite(0x0E,0)) { fault("MODE_SETUP"); return; }
    if (!flushFifo()) { fault("FIFO_RESET"); return; }
    running=true; lastPing=millis(); screenState="CONNECTED";
    screenDetail="Calibrate on laptop"; emit("ACK,START"); return;
  }
  if (s == "ARM") {
    if (diagnostic && !rtfThree) { emit("ERROR,DIAGNOSTIC_ONLY"); return; }
    if (running && !armed) { armed=true; warned=false; armAt=millis(); emit("ACK,ARM"); }
    else emit("ERROR,ARM_STATE");
    return;
  }
  if (s=="DIAG" || s=="RTF3") {
    if (armed || running) { emit("ERROR,STOP_FIRST"); return; }
    usingWifi=wifi;
    healthy=initializeSensor();
    if(!healthy) { emit("FAULT,SENSOR_INIT"); return; }
    diagnostic=true; rtfThree=(s=="RTF3"); redLed=irLed=greenLed=32;
    // MAX30101 multi-LED slots: RED, IR, GREEN. One sample contains nine bytes.
    if(!regWrite(0x09,0x87)||!regWrite(0x11,0x21)||!regWrite(0x12,0x03)
       ||!regWrite(0x0C,32)||!regWrite(0x0D,32)||!regWrite(0x0E,32)||!flushFifo()) {
      fault("DIAG_SETUP");return;
    }
    running=true;lastPing=millis();screenState=rtfThree?"RTF 3 CHANNEL":"DIAGNOSTIC ONLY";screenDetail=rtfThree?"Calibrate on laptop":"RED IR GREEN / no RTF";
    emit(rtfThree?"ACK,RTF3,100,RED_IR_GREEN":"ACK,DIAG,100,RED_IR_GREEN");return;
  }
  if(s.startsWith("LED3,")) {
    int r,i,g;char extra;
    if(!diagnostic || armed || !running) { emit("ERROR,DIAG_REQUIRED");return; }
    if(sscanf(s.c_str(),"LED3,%d,%d,%d%c",&r,&i,&g,&extra)!=3 || r<1||r>200||i<1||i>200||g<1||g>200) {emit("ERROR,LED_RANGE");return;}
    if(!regWrite(0x0C,r)||!regWrite(0x0D,i)||!regWrite(0x0E,g)){fault("I2C_LED");return;}
    redLed=r;irLed=i;greenLed=g;
    if(!flushFifo()){fault("FIFO_RESET");return;}
    emit(String("ACK,LED3,")+r+","+i+","+g+","+epoch);return;
  }
  if (s.startsWith("LED,")) {
    int r=0, i=0; char extra;
    if (armed) { emit("ERROR,LEDS_LOCKED"); return; }
    if (sscanf(s.c_str(), "LED,%d,%d%c", &r, &i, &extra) != 2 || r<1 || r>200 || i<1 || i>200) {
      emit("ERROR,LED_RANGE"); return;
    }
    if (!regWrite(0x0C,r) || !regWrite(0x0D,i)) { fault("I2C_LED"); return; }
    redLed=r; irLed=i;
    if (!flushFifo()) { fault("FIFO_RESET"); return; }
    emit(String("ACK,LED,")+r+","+i+","+epoch); return;
  }
  if (s.startsWith("STATUS,")) {
    if (!running) return; // Never overwrite a local STOP/disconnect warning with stale host status.
    int k=s.indexOf(',',7);
    screenState=(k<0?s.substring(7):s.substring(7,k)).substring(0,22);
    screenDetail=(k<0?"":s.substring(k+1)).substring(0,64);
    if (screenState=="OCCLUDED") screenState="PULSE SUPPRESSED";
    if (screenState=="RTF") screenState="PULSE RETURN";
    return;
  }
  emit("ERROR,UNKNOWN_COMMAND");
}
void readCommands(Stream &stream, String &line, bool wifi) {
  int budget=128;
  while (stream.available() && budget-- > 0) {
    char c=stream.read();
    if (c=='\n') { command(line,wifi); line=""; }
    else if (c!='\r') {
      if (line.length()>=256) { line=""; fault("COMMAND_TOO_LONG"); }
      else line+=c;
    }
  }
}
void setup() {
  Serial.begin(115200);
  Wire.begin(4,5); Wire.setClock(400000); Wire.setTimeOut(25);
  pinMode(33,OUTPUT); digitalWrite(33,HIGH); // Keep optional TFT SD card deselected.
  SPI.begin(18,19,23,27); // Explicit CS avoids the default GPIO5, used by I2C.
  tft.initR(TFT_TAB); tft.setRotation(0); tft.setTextWrap(true);
  // 100 samples/s, no FIFO averaging, 18-bit/411us, 16384nA range, RED+IR.
  healthy=initializeSensor();
  WiFi.persistent(false); WiFi.mode(WIFI_AP_STA); WiFi.setSleep(false);
  WiFi.setHostname("rtf-esp32"); WiFi.setAutoReconnect(true);
  WiFi.softAP(AP_NAME,AP_PASSWORD,6,false,1);
  networkPrefs.begin("rtf-network",false);
  String savedSsid=networkPrefs.getString("ssid","");
  if(savedSsid.length()) {
    String savedPass=networkPrefs.getString("password","");
    WiFi.begin(savedSsid.c_str(),savedPass.c_str());
  }
  server.begin(); server.setNoDelay(true);
  if (!healthy) { screenState="SENSOR ERROR"; screenDetail="Check MAX30101 wiring"; }
  drawScreen();
}
void loop() {
  uint32_t now=millis();
  if (!mdnsReady && WiFi.status()==WL_CONNECTED && !armed) {
    mdnsReady=MDNS.begin("rtf-esp32");
    if(mdnsReady) MDNS.addService("rtf","tcp",8765);
  }
  if (!client.connected()) {
    if (running && usingWifi) fault("WIFI_LOST");
    WiFiClient next=server.available();
    if (next) { client=next; client.setNoDelay(true); netLine=""; outbound=""; }
  }
  if (Serial) readCommands(Serial,usbLine,false);
  if (client.connected()) readCommands(client,netLine,true);
  // Commands can update armAt/lastPing after the loop began. Refresh now so
  // unsigned subtraction cannot mistake a newer timestamp for a long timeout.
  now=millis();
  if (armed && !warned && now-armAt>=WARN_MS) {
    warned=true; emit("WARN,CUFF_60_SECONDS");
  }
  if (armed && now-armAt>=ABORT_MS && screenState!="STOP / RELEASE") fault("CUFF_90_SECONDS");
  if (running && now-lastPing>5000) fault("HOST_HEARTBEAT_LOST");
  if (running && now-lastSample>1000) fault("SENSOR_STALLED");
  if (running && now-lastPoll>200) fault("ACQUISITION_PAUSE");
  if (running) {
    lastPoll=now;
    uint8_t ptr[3], status=0;
    if (!regRead(0x00,&status,1) || !regRead(0x04,ptr,3)) fault("I2C_READ");
    else if (status&0x20) fault("AMBIENT_OVERFLOW");
    else if (ptr[1]&0x1F) fault("FIFO_OVERFLOW");
    else {
      uint8_t count=(ptr[0]-ptr[2])&0x1F;
      if (!count && (status&0x80)) { fault("FIFO_FULL_AMBIGUOUS"); }
      for (uint8_t j=0;j<count && running;j++) {
        uint8_t b[9];
        if (!regRead(0x07,b,diagnostic?9:6)) { fault("I2C_FIFO"); break; }
        uint32_t r=((uint32_t(b[0])<<16)|(uint32_t(b[1])<<8)|b[2])&0x3FFFF;
        uint32_t i=((uint32_t(b[3])<<16)|(uint32_t(b[4])<<8)|b[5])&0x3FFFF;
        lastSample=millis();
        char row[100];
        if(diagnostic) {
          uint32_t g=((uint32_t(b[6])<<16)|(uint32_t(b[7])<<8)|b[8])&0x3FFFF;
          snprintf(row,sizeof(row),"D,%lu,%lu,%lu,%lu,%lu,%lu,%u,%u,%u",(unsigned long)seq++,(unsigned long)lastSample,(unsigned long)epoch,(unsigned long)r,(unsigned long)i,(unsigned long)g,redLed,irLed,greenLed);
        } else snprintf(row,sizeof(row),"S,%lu,%lu,%lu,%lu,%lu,%u,%u",
          (unsigned long)seq++,(unsigned long)lastSample,(unsigned long)epoch,
          (unsigned long)r,(unsigned long)i,redLed,irLed);
        emit(row);
      }
    }
    if (now-lastConfigCheck>1000 && running) {
      lastConfigCheck=now; uint8_t mode=0,config=0,leds[2]={0,0};
      if (!regRead(0x09,&mode,1)||!regRead(0x0A,&config,1)||!regRead(0x0C,leds,2)
          ||mode!=(diagnostic?7:3)||config!=0x67||leds[0]!=redLed||leds[1]!=irLed) {
        emit(String("CONFIG_OBSERVED,")+mode+","+config+","+leds[0]+","+leds[1]);
        fault("CONFIG_CHANGED");
      }
      if(diagnostic && running) {
        uint8_t green=0,slots[2];
        if(!regRead(0x0E,&green,1)||!regRead(0x11,slots,2)||green!=greenLed||slots[0]!=0x21||slots[1]!=3)fault("DIAG_CONFIG_CHANGED");
      }
    }
  }
  pumpNetwork();
  if (now-lastScreen>=1000) { lastScreen=now; drawScreen(); }
  delay(1);
}
