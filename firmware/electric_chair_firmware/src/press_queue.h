#ifndef PRESS_QUEUE_H
#define PRESS_QUEUE_H

#include <stdint.h>

static const uint8_t PRESS_QUEUE_CAPACITY = 8;

struct PressJob {
    uint8_t code;
    uint8_t hold_ticks;
    uint8_t gap_ticks;
    uint16_t seq;
};

struct PressTick {
    uint8_t output;
    bool emit;
    bool ack;
    bool done;
    PressJob job;
};

struct HoldTick {
    uint8_t output;
    bool emit;
    bool done;
    PressJob job;
};

class HoldEngine {
public:
    HoldEngine()
        : active_(false),
          stopping_(false),
          elapsed_ticks_(0),
          max_ticks_(0),
          stop_gap_ticks_(0),
          stop_gap_remaining_(0),
          active_job_{0, 0, 0, 0} {}

    bool start(uint8_t code, uint16_t seq, uint8_t max_ticks, uint8_t stop_gap_ticks) {
        if (active_) {
            return false;
        }
        active_job_ = {
            code,
            0,
            stop_gap_ticks ? stop_gap_ticks : static_cast<uint8_t>(1),
            seq,
        };
        active_ = true;
        stopping_ = false;
        elapsed_ticks_ = 0;
        max_ticks_ = max_ticks ? max_ticks : static_cast<uint8_t>(1);
        stop_gap_ticks_ = active_job_.gap_ticks;
        stop_gap_remaining_ = 0;
        return true;
    }

    bool stop() {
        if (!active_) {
            return false;
        }
        if (!stopping_) {
            stopping_ = true;
            stop_gap_remaining_ = stop_gap_ticks_;
        }
        return true;
    }

    HoldTick tick() {
        HoldTick result = {0x00, false, false, active_job_};
        if (!active_) {
            return result;
        }
        if (!stopping_ && elapsed_ticks_ >= max_ticks_) {
            stop();
        }
        if (stopping_) {
            result.output = 0x00;
            result.emit = true;
            if (stop_gap_remaining_ > 0) {
                stop_gap_remaining_--;
            }
            if (stop_gap_remaining_ == 0) {
                result.done = true;
                active_ = false;
                stopping_ = false;
            }
            return result;
        }
        result.output = active_job_.code;
        result.emit = true;
        elapsed_ticks_++;
        return result;
    }

    void clear() {
        active_ = false;
        stopping_ = false;
        elapsed_ticks_ = 0;
        max_ticks_ = 0;
        stop_gap_ticks_ = 0;
        stop_gap_remaining_ = 0;
        active_job_ = {0, 0, 0, 0};
    }

    bool active() const {
        return active_;
    }

    bool stopping() const {
        return stopping_;
    }

    const PressJob &job() const {
        return active_job_;
    }

private:
    bool active_;
    bool stopping_;
    uint8_t elapsed_ticks_;
    uint8_t max_ticks_;
    uint8_t stop_gap_ticks_;
    uint8_t stop_gap_remaining_;
    PressJob active_job_;
};

class PressQueue {
public:
    PressQueue() : jobs_(), head_(0), tail_(0), count_(0) {}

    bool push(const PressJob &job) {
        if (count_ >= PRESS_QUEUE_CAPACITY) {
            return false;
        }
        jobs_[tail_] = job;
        tail_ = static_cast<uint8_t>((tail_ + 1) % PRESS_QUEUE_CAPACITY);
        count_++;
        return true;
    }

    bool pop(PressJob &job) {
        if (count_ == 0) {
            return false;
        }
        job = jobs_[head_];
        head_ = static_cast<uint8_t>((head_ + 1) % PRESS_QUEUE_CAPACITY);
        count_--;
        return true;
    }

    void clear() {
        head_ = 0;
        tail_ = 0;
        count_ = 0;
    }

    uint8_t size() const {
        return count_;
    }

private:
    PressJob jobs_[PRESS_QUEUE_CAPACITY];
    uint8_t head_;
    uint8_t tail_;
    uint8_t count_;
};

class PressEngine {
public:
    PressEngine() : phase_(IDLE), ticks_remaining_(0), active_{0, 0, 0, 0} {}

    bool enqueue(uint8_t code, uint8_t hold_ticks, uint8_t gap_ticks, uint16_t seq) {
        PressJob job = {
            code,
            hold_ticks ? hold_ticks : static_cast<uint8_t>(1),
            gap_ticks ? gap_ticks : static_cast<uint8_t>(1),
            seq,
        };
        return queue_.push(job);
    }

    PressTick tick() {
        PressTick result = {0x00, false, false, false, active_};

        if (phase_ == IDLE) {
            if (!queue_.pop(active_)) {
                return result;
            }
            phase_ = HOLDING;
            ticks_remaining_ = active_.hold_ticks;
            result.ack = true;
            result.job = active_;
        }

        if (phase_ == HOLDING) {
            result.output = active_.code;
            result.emit = true;
            result.job = active_;
            if (ticks_remaining_ > 0) {
                ticks_remaining_--;
            }
            if (ticks_remaining_ == 0) {
                phase_ = GAPPING;
                ticks_remaining_ = active_.gap_ticks;
            }
            return result;
        }

        if (phase_ == GAPPING) {
            result.output = 0x00;
            result.emit = true;
            result.job = active_;
            if (ticks_remaining_ > 0) {
                ticks_remaining_--;
            }
            if (ticks_remaining_ == 0) {
                result.done = true;
                phase_ = IDLE;
            }
        }

        return result;
    }

    void clear() {
        queue_.clear();
        phase_ = IDLE;
        ticks_remaining_ = 0;
        active_ = {0, 0, 0, 0};
    }

    uint8_t queued() const {
        return queue_.size();
    }

private:
    enum Phase : uint8_t {
        IDLE,
        HOLDING,
        GAPPING,
    };

    PressQueue queue_;
    Phase phase_;
    uint8_t ticks_remaining_;
    PressJob active_;
};

#endif
