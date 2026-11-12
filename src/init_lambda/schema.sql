
CREATE TABLE IF NOT EXISTS users (
    user_id CHAR(36) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    role ENUM('PATIENT', 'DOCTOR', 'ADMIN') NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at DATETIME(3),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS patients (
    patient_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20),
    date_of_birth DATE NOT NULL,
    gender ENUM('MALE', 'FEMALE', 'OTHER'),
    city VARCHAR(100) NOT NULL DEFAULT '',
    emergency_contact_name VARCHAR(150),
    emergency_contact_phone VARCHAR(20),
    notes TEXT,
    profile_image_key VARCHAR(255),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_user (user_id),
    KEY idx_patients_name (last_name, first_name),
    CONSTRAINT fk_patients_user FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE CASCADE,
    CONSTRAINT ck_patients_gender CHECK (
        gender IS NULL OR gender IN ('MALE', 'FEMALE', 'OTHER')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id CHAR(36) NOT NULL,
    name VARCHAR(200) NOT NULL,
    address VARCHAR(500) NOT NULL,
    city VARCHAR(100) NOT NULL DEFAULT '',
    phone_number VARCHAR(20),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (hospital_id),
    UNIQUE KEY uq_hospital_name_address (name, address),
    KEY idx_hospitals_city_id (city, hospital_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO hospitals (hospital_id, name, address) VALUES
    ('a0000000-0000-0000-0000-000000000001', 'Central City Medical Center', '100 Health Avenue, Bengaluru, Karnataka 560001'),
    ('a0000000-0000-0000-0000-000000000002', 'Northside Specialty Hospital', '25 Lake View Road, Bengaluru, Karnataka 560038'),
    ('a0000000-0000-0000-0000-000000000003', 'Riverside Community Hospital', '8 River Road, Mysuru, Karnataka 570001');


CREATE TABLE IF NOT EXISTS doctors (
    doctor_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    hospital_id CHAR(36) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    license_number VARCHAR(50) NOT NULL,
    experience_years SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    phone_number VARCHAR(20),
    qualification VARCHAR(500),
    profile_image_key VARCHAR(255),
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    languages TEXT,
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctors_user (user_id),
    UNIQUE KEY uq_doctors_license (license_number),
    KEY idx_doctors_name (last_name, first_name),
    KEY idx_doctors_hospital (hospital_id),
    KEY idx_doctors_verified_experience (is_verified, experience_years, doctor_id),
    CONSTRAINT fk_doctors_user FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE CASCADE,
    CONSTRAINT fk_doctors_hospital FOREIGN KEY (hospital_id)
        REFERENCES hospitals(hospital_id),
    CONSTRAINT ck_doctors_experience CHECK (experience_years >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS specializations (
    specialization_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (specialization_id),
    UNIQUE KEY uq_specializations_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO specializations (name) VALUES
    ('General Medicine'),
    ('Cardiology'),
    ('Dermatology'),
    ('Endocrinology'),
    ('Neurology'),
    ('Obstetrics and Gynaecology'),
    ('Paediatrics'),
    ('Psychiatry'),
    ('Orthopaedics'),
    ('Pulmonology');


CREATE TABLE IF NOT EXISTS doctor_specializations (
    doctor_id CHAR(36) NOT NULL,
    specialization_id INT UNSIGNED NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (doctor_id, specialization_id),
    KEY idx_doctor_specializations_specialization (specialization_id, doctor_id),
    CONSTRAINT fk_doctor_specializations_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_doctor_specializations_specialization FOREIGN KEY (specialization_id)
        REFERENCES specializations(specialization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS consultation_catalog (
    consultation_type_id VARCHAR(32) NOT NULL,
    name VARCHAR(80) NOT NULL,
    mode ENUM('IN_PERSON') NOT NULL DEFAULT 'IN_PERSON',
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_catalog_name (name),
    CONSTRAINT ck_consultation_catalog_duration CHECK (duration_minutes > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO consultation_catalog (
    consultation_type_id, name, mode, duration_minutes
) VALUES
    ('QUICK', 'QUICK', 'IN_PERSON', 15),
    ('STANDARD', 'STANDARD', 'IN_PERSON', 30),
    ('DETAILED', 'DETAILED', 'IN_PERSON', 60),
    ('HEALTH_CAMP', 'HEALTH_CAMP', 'IN_PERSON', 60);


CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    catalog_type_id VARCHAR(32) NOT NULL,
    fee DECIMAL(10,2) NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_type_doctor_catalog (doctor_id, catalog_type_id),
    KEY idx_consultation_types_doctor_active (doctor_id, is_active),
    KEY idx_consultation_types_active_fee (is_active, fee, doctor_id),
    KEY idx_consultation_types_catalog (catalog_type_id),
    CONSTRAINT fk_consultation_types_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_consultation_types_catalog FOREIGN KEY (catalog_type_id)
        REFERENCES consultation_catalog(consultation_type_id),
    CONSTRAINT ck_consultation_type_fee CHECK (fee >= 0),
    CONSTRAINT ck_consultation_type_capacity CHECK (capacity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS availability_patterns (
    pattern_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (pattern_id),
    UNIQUE KEY uq_availability_type_day_start (consultation_type_id, day_of_week, start_time),
    KEY idx_availability_day_start (day_of_week, start_time),
    CONSTRAINT fk_availability_consultation_type FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id) ON DELETE CASCADE,
    CONSTRAINT ck_availability_day CHECK (day_of_week BETWEEN 0 AND 6),
    CONSTRAINT ck_availability_time CHECK (end_time > start_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    start_datetime DATETIME(3) NOT NULL,
    end_datetime DATETIME(3) NOT NULL,
    reason VARCHAR(200),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (blackout_id),
    KEY idx_blackouts_doctor_range (doctor_id, start_datetime, end_datetime),
    CONSTRAINT fk_blackouts_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id) ON DELETE CASCADE,
    CONSTRAINT ck_blackouts_range CHECK (end_datetime > start_datetime)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS appointments (
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    appointment_datetime DATETIME(3) NOT NULL,
    appointment_status ENUM('PENDING', 'CONFIRMED', 'CANCELLED', 'COMPLETED', 'NO_SHOW')
        NOT NULL DEFAULT 'PENDING',
    active_patient_slot VARCHAR(150)
        GENERATED ALWAYS AS (
            IF(
                appointment_status <> 'CANCELLED',
                CONCAT(patient_id, '#', consultation_type_id, '#',
                    DATE_FORMAT(appointment_datetime, '%Y-%m-%d %H:%i:%s.%f')),
                NULL
            )
        ) STORED,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_active_patient_slot (active_patient_slot),
    KEY idx_appointments_patient_time (patient_id, appointment_datetime),
    KEY idx_appointments_type_time_status (consultation_type_id, appointment_datetime, appointment_status),
    KEY idx_appointments_reminders (appointment_status, appointment_datetime),
    CONSTRAINT fk_appointments_patient FOREIGN KEY (patient_id)
        REFERENCES patients(patient_id),
    CONSTRAINT fk_appointments_consultation_type FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS payments (
    payment_id CHAR(36) NOT NULL,
    appointment_id CHAR(36) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    payment_status ENUM('INITIATED', 'SUCCESS', 'FAILED') NOT NULL DEFAULT 'INITIATED',
    payment_method VARCHAR(50) NOT NULL,
    transaction_id VARCHAR(255),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (payment_id),
    UNIQUE KEY uq_payments_transaction (transaction_id),
    KEY idx_payments_appointment (appointment_id, created_at),
    CONSTRAINT fk_payments_appointment FOREIGN KEY (appointment_id)
        REFERENCES appointments(appointment_id),
    CONSTRAINT ck_payments_amount CHECK (amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS refunds (
    refund_id CHAR(36) NOT NULL,
    payment_id CHAR(36) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    refund_status ENUM('PENDING', 'PROCESSED', 'FAILED') NOT NULL DEFAULT 'PENDING',
    refund_transaction_id VARCHAR(255),
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refunds_transaction (refund_transaction_id),
    KEY idx_refunds_payment (payment_id, created_at),
    CONSTRAINT fk_refunds_payment FOREIGN KEY (payment_id)
        REFERENCES payments(payment_id),
    CONSTRAINT ck_refunds_amount CHECK (refund_amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
