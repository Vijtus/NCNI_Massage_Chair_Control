#include <unity.h>

#include "press_queue.h"

void test_queue_preserves_rapid_presses() {
    PressEngine engine;

    for (uint8_t i = 0; i < 5; ++i) {
        TEST_ASSERT_TRUE(engine.enqueue(static_cast<uint8_t>(0x10 + i), 3, 3, i + 1));
    }

    uint8_t acked = 0;
    uint8_t done = 0;
    uint8_t emitted_ticks = 0;
    uint8_t non_zero_ticks = 0;

    for (uint8_t tick = 0; tick < 40; ++tick) {
        PressTick result = engine.tick();
        if (result.ack) {
            acked++;
        }
        if (result.done) {
            done++;
        }
        if (result.emit) {
            emitted_ticks++;
        }
        if (result.output != 0x00) {
            non_zero_ticks++;
        }
    }

    TEST_ASSERT_EQUAL_UINT8(5, acked);
    TEST_ASSERT_EQUAL_UINT8(5, done);
    TEST_ASSERT_EQUAL_UINT8(30, emitted_ticks);
    TEST_ASSERT_EQUAL_UINT8(15, non_zero_ticks);
}

void test_idle_does_not_emit_uart_bytes() {
    PressEngine engine;
    PressTick idle = engine.tick();

    TEST_ASSERT_FALSE(idle.emit);
    TEST_ASSERT_EQUAL_UINT8(0x00, idle.output);
    TEST_ASSERT_FALSE(idle.ack);
    TEST_ASSERT_FALSE(idle.done);
}

void test_queue_capacity_is_bounded() {
    PressEngine engine;
    for (uint8_t i = 0; i < PRESS_QUEUE_CAPACITY; ++i) {
        TEST_ASSERT_TRUE(engine.enqueue(0x13, 3, 3, i + 1));
    }
    TEST_ASSERT_FALSE(engine.enqueue(0x13, 3, 3, 99));
}

void test_hold_engine_emits_until_stop_then_neutral_done() {
    HoldEngine engine;
    TEST_ASSERT_TRUE(engine.start(0x09, 42, 80, 3));

    HoldTick first = engine.tick();
    TEST_ASSERT_TRUE(first.emit);
    TEST_ASSERT_EQUAL_UINT8(0x09, first.output);
    TEST_ASSERT_FALSE(first.done);

    TEST_ASSERT_TRUE(engine.stop());
    for (uint8_t i = 0; i < 2; ++i) {
        HoldTick gap = engine.tick();
        TEST_ASSERT_TRUE(gap.emit);
        TEST_ASSERT_EQUAL_UINT8(0x00, gap.output);
        TEST_ASSERT_FALSE(gap.done);
    }
    HoldTick done = engine.tick();
    TEST_ASSERT_TRUE(done.emit);
    TEST_ASSERT_EQUAL_UINT8(0x00, done.output);
    TEST_ASSERT_TRUE(done.done);
    TEST_ASSERT_FALSE(engine.active());
}

void test_hold_engine_hard_timeout_forces_neutral_done() {
    HoldEngine engine;
    TEST_ASSERT_TRUE(engine.start(0x06, 77, 2, 2));

    TEST_ASSERT_EQUAL_UINT8(0x06, engine.tick().output);
    TEST_ASSERT_EQUAL_UINT8(0x06, engine.tick().output);

    HoldTick first_gap = engine.tick();
    TEST_ASSERT_TRUE(first_gap.emit);
    TEST_ASSERT_EQUAL_UINT8(0x00, first_gap.output);
    TEST_ASSERT_FALSE(first_gap.done);

    HoldTick done = engine.tick();
    TEST_ASSERT_TRUE(done.emit);
    TEST_ASSERT_EQUAL_UINT8(0x00, done.output);
    TEST_ASSERT_TRUE(done.done);
    TEST_ASSERT_FALSE(engine.active());
}

void test_hold_engine_allows_only_one_active_hold() {
    HoldEngine engine;
    TEST_ASSERT_TRUE(engine.start(0x06, 1, 80, 3));
    TEST_ASSERT_FALSE(engine.start(0x09, 2, 80, 3));
}

int main(int argc, char **argv) {
    UNITY_BEGIN();
    RUN_TEST(test_idle_does_not_emit_uart_bytes);
    RUN_TEST(test_queue_preserves_rapid_presses);
    RUN_TEST(test_queue_capacity_is_bounded);
    RUN_TEST(test_hold_engine_emits_until_stop_then_neutral_done);
    RUN_TEST(test_hold_engine_hard_timeout_forces_neutral_done);
    RUN_TEST(test_hold_engine_allows_only_one_active_hold);
    return UNITY_END();
}
