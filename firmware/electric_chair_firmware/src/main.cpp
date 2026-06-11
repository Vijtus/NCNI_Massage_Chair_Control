#include <Arduino.h>
#include <avr/pgmspace.h>
#include "SoftwareSerial.h"
#include "press_queue.h"

static const char CMD_RAMIONA[] PROGMEM = "ramiona";
static const char CMD_PRZEDRAMIONA[] PROGMEM = "przedramiona";
static const char CMD_NOGI[] PROGMEM = "nogi";
static const char CMD_SILA_PLUS[] PROGMEM = "sila_nacisku_plus";
static const char CMD_SILA_MINUS[] PROGMEM = "sila_nacisku_minus";
static const char CMD_MASAZ_POSLADKOW[] PROGMEM = "masaz_posladkow";
static const char CMD_MASAZ_STOP[] PROGMEM = "masaz_stop";
static const char CMD_PREDKOSC_STOP[] PROGMEM = "predkosc_masazu_stop";
static const char CMD_PAUZA[] PROGMEM = "pauza";
static const char CMD_OGRZEWANIE[] PROGMEM = "ogrzewanie";
static const char CMD_CZAS[] PROGMEM = "czas";
static const char CMD_CALE_CIALO[] PROGMEM = "masaz_calego_ciala";
static const char CMD_ZERO[] PROGMEM = "grawitacja_zero";
static const char CMD_AUTO[] PROGMEM = "tryb_automatyczny";
static const char CMD_OPARCIE_GORA[] PROGMEM = "oparcie_w_gore";
static const char CMD_OPARCIE_DOL[] PROGMEM = "oparcie_w_dol";
static const char CMD_PREDKOSC_MINUS[] PROGMEM = "predkosc_minus";
static const char CMD_PREDKOSC_PLUS[] PROGMEM = "predkosc_plus";
static const char CMD_DO_TYLU_1[] PROGMEM = "do_przodu_do_tylu_1";
static const char CMD_PLECY[] PROGMEM = "plecy_i_talia";
static const char CMD_DO_TYLU_2[] PROGMEM = "do_przodu_do_tylu_2";
static const char CMD_SZYJA[] PROGMEM = "szyja";
static const char CMD_POWER[] PROGMEM = "power";

static const uint32_t POLL_INTERVAL_MS = 100;
static const uint8_t DEFAULT_HOLD_TICKS = 3;
static const uint8_t DEFAULT_GAP_TICKS = 3;
static const uint8_t BACKREST_HOLD_MAX_TICKS = 80; // 8s at 100ms/tick
static const uint8_t BACKREST_HOLD_STOP_GAP_TICKS = 3;
static const uint8_t LINE_BUFFER_SIZE = 48;
static const uint8_t CHAIR_FRAME_SIZE = 33;
static const char USB_LISTEN_OFF_CONTROL = '!';
static const char USB_LISTEN_ON_CONTROL = '~';
// Release control: bridge sends '#' to release pins 10/11 back to
// high-impedance INPUT so the chair's OEM panel can drive the bus again.
// Next inbound USB byte (other than '#') will re-engage SoftwareSerial.
static const char USB_RELEASE_CONTROL = '#';

static const uint8_t SOFTSERIAL_RX_PIN = 10;
static const uint8_t SOFTSERIAL_TX_PIN = 11;

static SoftwareSerial mySerial(SOFTSERIAL_RX_PIN, SOFTSERIAL_TX_PIN);
static PressEngine press_engine;
static HoldEngine hold_engine;
static char line_buffer[LINE_BUFFER_SIZE];
static uint8_t chair_frame[CHAIR_FRAME_SIZE];
static uint8_t chair_frame_length = 0;
static uint8_t line_length = 0;
static uint32_t last_send_ms = 0;
static bool chair_read_enabled = false;
// SoftwareSerial.begin() is deferred until the first USB byte arrives.
// Until then, pins 10/11 are pinMode(INPUT) (high-impedance), so the chair's
// OEM panel can drive the bus unopposed. The first byte from the bridge
// flips this latch and the Arduino takes over.
static bool ssbegin_done = false;

static void print_hex_byte(uint8_t value) {
    Serial.print(F("0x"));
    if (value < 0x10) {
        Serial.print('0');
    }
    Serial.print(value, HEX);
}

static void print_hex_pair(uint8_t value) {
    if (value < 0x10) {
        Serial.print('0');
    }
    Serial.print(value, HEX);
}

static char lower_ascii(char value) {
    if (value >= 'A' && value <= 'Z') {
        return static_cast<char>(value + ('a' - 'A'));
    }
    return value;
}

static bool equals_ignore_case(const char *left, const char *right) {
    while (*left && *right) {
        if (lower_ascii(*left) != lower_ascii(*right)) {
            return false;
        }
        left++;
        right++;
    }
    return *left == '\0' && *right == '\0';
}

static bool progmem_equals_ignore_case(const char *ram, const char *progmem) {
    uint8_t index = 0;
    while (true) {
        char expected = static_cast<char>(pgm_read_byte(progmem + index));
        char actual = ram[index];
        if (expected == '\0' || actual == '\0') {
            return expected == '\0' && actual == '\0';
        }
        if (lower_ascii(actual) != lower_ascii(expected)) {
            return false;
        }
        index++;
    }
}

static void trim_in_place(char *line) {
    char *start = line;
    while (*start == ' ' || *start == '\t') {
        start++;
    }
    if (start != line) {
        char *dst = line;
        while (*start) {
            *dst++ = *start++;
        }
        *dst = '\0';
    }
    uint8_t len = 0;
    while (line[len]) {
        len++;
    }
    while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t')) {
        line[--len] = '\0';
    }
}

static bool parse_seq_prefix(char *line, uint16_t &seq, char *&name) {
    seq = 0;
    name = line;
    if (line[0] < '0' || line[0] > '9') {
        return false;
    }

    uint32_t parsed = 0;
    uint8_t index = 0;
    while (line[index] >= '0' && line[index] <= '9') {
        parsed = (parsed * 10UL) + static_cast<uint32_t>(line[index] - '0');
        if (parsed > 65535UL) {
            parsed = 65535UL;
        }
        index++;
    }
    if (line[index] != ' ' && line[index] != '\t') {
        return false;
    }
    while (line[index] == ' ' || line[index] == '\t') {
        index++;
    }
    seq = static_cast<uint16_t>(parsed);
    name = line + index;
    return true;
}

static void ensure_softserial_started() {
    if (ssbegin_done) {
        return;
    }
    // SoftwareSerial::begin() does NOT touch pinMode — only the
    // constructor does, and that ran before setup() forced pin 11 to
    // INPUT. So after begin() we must explicitly restore pin 11 to
    // OUTPUT HIGH (UART idle level). Without this, mySerial.write() is
    // a no-op on the wire: ACK/DONE come back from this Arduino's own
    // print path but no UART frame ever reaches the chair.
    mySerial.begin(9600);
    digitalWrite(SOFTSERIAL_TX_PIN, HIGH);
    pinMode(SOFTSERIAL_TX_PIN, OUTPUT);
    mySerial.stopListening();
    ssbegin_done = true;
    Serial.println(F("SoftSerial: bridge took over pins 10/11"));
}

static void release_softserial() {
    // Bridge handed the bus back. Stop listening, drop pin 11 to
    // INPUT/high-Z (and pin 10 stays INPUT), reset the latch. The chair's
    // OEM panel can drive the bus again. Any subsequent USB byte other
    // than another '#' will re-engage SoftwareSerial via
    // ensure_softserial_started().
    if (ssbegin_done) {
        if (hold_engine.active()) {
            mySerial.write(static_cast<uint8_t>(0x00));
        }
        mySerial.stopListening();
        mySerial.end();
    }
    hold_engine.clear();
    press_engine.clear();
    pinMode(SOFTSERIAL_TX_PIN, INPUT);
    pinMode(SOFTSERIAL_RX_PIN, INPUT);
    chair_read_enabled = false;
    chair_frame_length = 0;
    ssbegin_done = false;
    Serial.println(F("SoftSerial released: pins 10/11 high-Z"));
}

static void set_chair_read(bool enabled) {
    chair_read_enabled = enabled;
    if (ssbegin_done) {
        if (chair_read_enabled) {
            mySerial.listen();
        } else {
            mySerial.stopListening();
        }
    }
    Serial.print(F("Chair read: "));
    Serial.println(chair_read_enabled ? F("ON") : F("OFF"));
}

static bool handle_control_command(const char *cmd) {
    if (equals_ignore_case(cmd, "listen") || equals_ignore_case(cmd, "listen toggle")) {
        set_chair_read(!chair_read_enabled);
        return true;
    }
    if (equals_ignore_case(cmd, "listen on")) {
        set_chair_read(true);
        return true;
    }
    if (equals_ignore_case(cmd, "listen off")) {
        set_chair_read(false);
        return true;
    }
    return false;
}

static bool lookup_command(const char *name, uint8_t &code) {
    if (progmem_equals_ignore_case(name, CMD_POWER)) {
        code = 0x01;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PREDKOSC_STOP)) {
        code = 0x02;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_OGRZEWANIE)) {
        code = 0x03;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_CALE_CIALO)) {
        code = 0x04;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_AUTO)) {
        code = 0x05;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_OPARCIE_DOL)) {
        code = 0x06;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_CZAS)) {
        code = 0x07;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_ZERO)) {
        code = 0x08;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_OPARCIE_GORA)) {
        code = 0x09;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PAUZA)) {
        code = 0x0B;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_MASAZ_STOP)) {
        code = 0x0D;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_MASAZ_POSLADKOW)) {
        code = 0x0E;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_SILA_MINUS)) {
        code = 0x0F;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_SILA_PLUS)) {
        code = 0x10;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_NOGI)) {
        code = 0x11;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PRZEDRAMIONA)) {
        code = 0x12;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_RAMIONA)) {
        code = 0x13;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PREDKOSC_MINUS)) {
        code = 0x14;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PREDKOSC_PLUS)) {
        code = 0x15;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_DO_TYLU_1)) {
        code = 0x16;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_PLECY)) {
        code = 0x17;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_DO_TYLU_2)) {
        code = 0x18;
        return true;
    }
    if (progmem_equals_ignore_case(name, CMD_SZYJA)) {
        code = 0x19;
        return true;
    }
    return false;
}

static bool is_backrest_hold_code(uint8_t code) {
    return code == 0x06 || code == 0x09;
}

static void print_nack(uint16_t seq, uint8_t code, const __FlashStringHelper *reason) {
    Serial.print(F("NACK seq="));
    Serial.print(seq);
    Serial.print(F(" code="));
    print_hex_byte(code);
    Serial.print(F(" error="));
    Serial.println(reason);
}

static void print_ack_done(const __FlashStringHelper *label, const PressJob &job) {
    Serial.print(label);
    Serial.print(F(" seq="));
    Serial.print(job.seq);
    Serial.print(F(" code="));
    print_hex_byte(job.code);
    Serial.println();
}

static bool start_hold(uint16_t seq, uint8_t code) {
    if (!is_backrest_hold_code(code)) {
        print_nack(seq, code, F("not_holdable"));
        return false;
    }
    if (!hold_engine.start(
            code,
            seq,
            BACKREST_HOLD_MAX_TICKS,
            BACKREST_HOLD_STOP_GAP_TICKS
        )) {
        print_nack(seq, code, F("hold_active"));
        return false;
    }
    print_ack_done(F("ACK"), hold_engine.job());
    return true;
}

static void request_hold_stop(uint16_t seq, uint8_t code) {
    if (!is_backrest_hold_code(code)) {
        print_nack(seq, code, F("not_holdable"));
        return;
    }
    if (!hold_engine.active()) {
        return;
    }
    hold_engine.stop();
}

static void handle_line(char *line) {
    trim_in_place(line);
    if (line[0] == '\0') {
        return;
    }
    if (handle_control_command(line)) {
        return;
    }

    uint16_t seq = 0;
    char *name = line;
    bool has_seq = parse_seq_prefix(line, seq, name);
    trim_in_place(name);

    char *argument = NULL;
    for (uint8_t index = 0; name[index] != '\0'; ++index) {
        if (name[index] == ' ' || name[index] == '\t') {
            name[index] = '\0';
            argument = name + index + 1;
            trim_in_place(argument);
            break;
        }
    }

    if (equals_ignore_case(name, "hold_start") || equals_ignore_case(name, "hold_stop")) {
        uint8_t hold_code = 0x00;
        if (!argument || !lookup_command(argument, hold_code)) {
            print_nack(seq, 0x00, F("unknown_command"));
            return;
        }
        if (equals_ignore_case(name, "hold_start")) {
            start_hold(seq, hold_code);
        } else {
            request_hold_stop(seq, hold_code);
        }
        return;
    }

    uint8_t code = 0x00;
    if (!lookup_command(name, code)) {
        if (has_seq) {
            print_nack(seq, 0x00, F("unknown_command"));
        } else {
            Serial.print(F("Unknown command: "));
            Serial.println(name);
        }
        return;
    }

    if (is_backrest_hold_code(code)) {
        print_nack(seq, code, F("hold_action_required"));
        return;
    }
    if (hold_engine.active()) {
        print_nack(seq, code, F("hold_active"));
        return;
    }
    if (press_engine.enqueue(code, DEFAULT_HOLD_TICKS, DEFAULT_GAP_TICKS, seq)) {
        Serial.print(F("Queued seq="));
        Serial.print(seq);
        Serial.print(F(" code="));
        print_hex_byte(code);
        Serial.print(F(" name="));
        Serial.println(name);
        return;
    }

    print_nack(seq, code, F("queue_full"));
}

static void handle_serial_input() {
    while (Serial.available() > 0) {
        // Peek the next byte first so a '#' release control can be honored
        // without flipping the SoftSerial latch.
        char ch = static_cast<char>(Serial.read());
        if (ch == USB_RELEASE_CONTROL) {
            line_length = 0;
            release_softserial();
            continue;
        }
        // First non-release USB byte from the bridge ends the high-Z idle.
        ensure_softserial_started();
        if (ch == USB_LISTEN_OFF_CONTROL) {
            line_length = 0;
            set_chair_read(false);
            continue;
        }
        if (ch == USB_LISTEN_ON_CONTROL) {
            line_length = 0;
            set_chair_read(true);
            continue;
        }
        if (ch == '\n' || ch == '\r') {
            if (line_length > 0) {
                line_buffer[line_length] = '\0';
                handle_line(line_buffer);
                line_length = 0;
            }
            continue;
        }
        if (line_length + 1 < LINE_BUFFER_SIZE) {
            line_buffer[line_length++] = ch;
        } else {
            line_length = 0;
            Serial.println(F("ERR line_too_long"));
        }
    }
}

static void send_periodic() {
    uint32_t now = millis();
    if (now - last_send_ms < POLL_INTERVAL_MS) {
        return;
    }
    last_send_ms = now;

    HoldTick hold_tick = hold_engine.tick();
    if (hold_tick.emit) {
        ensure_softserial_started();
        mySerial.write(hold_tick.output);
        if (hold_tick.done) {
            print_ack_done(F("DONE"), hold_tick.job);
        }
        return;
    }

    PressTick tick = press_engine.tick();
    if (tick.ack) {
        print_ack_done(F("ACK"), tick.job);
    }
    if (tick.emit) {
        // Defensive: any path that ends up here came through handle_line()
        // which is reached only after handle_serial_input() has flipped
        // the latch. Still cheap to guard, since the queue could in
        // principle be primed in a future code path that bypasses USB.
        ensure_softserial_started();
        mySerial.write(tick.output);
    }
    if (tick.done) {
        print_ack_done(F("DONE"), tick.job);
    }
}

static void read_chair_data() {
    if (!chair_read_enabled || !ssbegin_done) {
        return;
    }

    for (uint8_t i = 0; i < 32 && mySerial.available() > 0; ++i) {
        if (Serial.available() > 0) {
            break;
        }
        uint8_t value = static_cast<uint8_t>(mySerial.read());
        if (chair_frame_length == 0 && value != 0xAA) {
            continue;
        }
        if (chair_frame_length == 1 && value != 0x55) {
            chair_frame_length = (value == 0xAA) ? 1 : 0;
            chair_frame[0] = 0xAA;
            continue;
        }
        chair_frame[chair_frame_length++] = value;
        if (chair_frame_length < CHAIR_FRAME_SIZE) {
            continue;
        }
        if (
            chair_frame[0] == 0xAA &&
            chair_frame[1] == 0x55 &&
            chair_frame[29] == 0x00 &&
            chair_frame[30] == 0x00 &&
            chair_frame[31] == 0x00 &&
            chair_frame[32] == 0x00
        ) {
            Serial.print(F("FRAME "));
            for (uint8_t index = 0; index < CHAIR_FRAME_SIZE; ++index) {
                print_hex_pair(chair_frame[index]);
            }
            Serial.println();
        }
        chair_frame_length = 0;
    }
}

void setup() {
    Serial.begin(115200);
    // Hold pins 10/11 in high-impedance INPUT until the bridge sends its
    // first USB byte. This lets the chair's OEM panel drive its own
    // SoftwareSerial bus unopposed when the computer is not talking.
    // ensure_softserial_started() flips the latch and calls mySerial.begin().
    pinMode(SOFTSERIAL_RX_PIN, INPUT);
    pinMode(SOFTSERIAL_TX_PIN, INPUT);
    Serial.println(F("Ready. Type '<seq> <command>' and press Enter."));
    Serial.println(F("Controls: listen | listen on | listen off | listen toggle | ! | ~ | #"));
    Serial.println(F("SoftSerial idle: pins 10/11 high-Z until first USB byte."));
}

void loop() {
    handle_serial_input();
    read_chair_data();
    send_periodic();
}
