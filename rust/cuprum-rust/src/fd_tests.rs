//! Unit and property tests for platform file descriptor conversion.

#[cfg(unix)]
mod unix {
    //! Tests for Unix file descriptor conversion boundaries.

    use proptest::prelude::*;

    use crate::convert_platform_fd;

    fn check_accepted(input: i64, expected: i32) {
        match convert_platform_fd(input) {
            Ok(fd) => assert_eq!(fd, expected),
            Err(err) => panic!("{input} should be accepted: {err}"),
        }
    }

    #[test]
    fn convert_fd_accepts_valid_descriptors() {
        check_accepted(0, 0);
        check_accepted(i64::from(i32::MAX), i32::MAX);
    }

    #[test]
    fn convert_fd_rejects_negative_descriptors() {
        assert_eq!(
            convert_platform_fd(-1),
            Err("file descriptor must be non-negative"),
        );
    }

    #[test]
    fn convert_fd_rejects_i64_min_descriptors_as_out_of_range() {
        assert_eq!(
            convert_platform_fd(i64::MIN),
            Err("file descriptor out of range"),
        );
    }

    #[test]
    fn convert_fd_rejects_out_of_range_descriptors() {
        assert_eq!(
            convert_platform_fd(i64::from(i32::MAX) + 1),
            Err("file descriptor out of range"),
        );
    }

    proptest! {
        #[test]
        fn convert_fd_matches_descriptor_bounds(value in any::<i64>()) {
            let result = convert_platform_fd(value);

            if let Ok(expected) = i32::try_from(value) {
                if expected >= 0 {
                    match result {
                        Ok(fd) => prop_assert_eq!(fd, expected),
                        Err(err) => prop_assert!(false, "{value} should be accepted: {err}"),
                    }
                } else {
                    prop_assert!(result.is_err());
                }
            } else {
                prop_assert!(result.is_err());
            }
        }
    }
}

#[cfg(windows)]
mod windows {
    //! Tests for Windows handle conversion boundaries.

    use proptest::prelude::*;

    use crate::convert_platform_fd;

    fn check_accepted(input: i64, expected: usize) {
        match convert_platform_fd(input) {
            Ok(handle) => assert_eq!(handle, expected),
            Err(err) => panic!("{input} should be accepted: {err}"),
        }
    }

    #[test]
    fn convert_fd_accepts_valid_handles() {
        check_accepted(0, 0_usize);
        check_accepted(i64::from(u16::MAX), usize::from(u16::MAX));
    }

    #[test]
    fn convert_fd_rejects_negative_handles() {
        assert_eq!(
            convert_platform_fd(-1),
            Err("file handle must be non-negative"),
        );
    }

    #[cfg(target_pointer_width = "32")]
    #[test]
    fn convert_fd_rejects_out_of_range_handles() {
        assert_eq!(
            convert_platform_fd(i64::from(u32::MAX) + 1),
            Err("file handle out of range"),
        );
    }

    proptest! {
        #[test]
        fn convert_fd_matches_handle_bounds(value in any::<i64>()) {
            let result = convert_platform_fd(value);

            if let Ok(expected) = usize::try_from(value) {
                match result {
                    Ok(handle) => prop_assert_eq!(handle, expected),
                    Err(err) => prop_assert!(false, "{value} should be accepted: {err}"),
                }
            } else {
                prop_assert!(result.is_err());
            }
        }
    }
}
