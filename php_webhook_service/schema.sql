CREATE DATABASE IF NOT EXISTS `zocs_playground`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'zocs_playground'@'localhost'
    IDENTIFIED BY 'zocs_playground';

ALTER USER 'zocs_playground'@'localhost'
    IDENTIFIED BY 'zocs_playground';

GRANT SELECT, INSERT ON `zocs_playground`.* TO 'zocs_playground'@'localhost';

USE `zocs_playground`;

CREATE TABLE IF NOT EXISTS `mijia_readings` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `measured_at` DATETIME(6) NOT NULL,
    `address` CHAR(17) NOT NULL,
    `model` VARCHAR(32) NOT NULL,
    `product_id` VARCHAR(10) NOT NULL,
    `temperature` DECIMAL(6, 2) NULL,
    `humidity` DECIMAL(6, 2) NULL,
    `battery` DECIMAL(6, 2) NULL,
    `rssi` DECIMAL(6, 2) NULL,
    `received_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    INDEX `idx_readings_address_id` (`address`, `id`),
    INDEX `idx_readings_measured_at` (`measured_at`)
) ENGINE=InnoDB;
