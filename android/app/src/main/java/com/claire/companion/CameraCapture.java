package com.claire.companion;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;

import androidx.annotation.NonNull;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageCapture;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.core.ImageProxy;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.core.content.ContextCompat;
import androidx.lifecycle.Lifecycle;
import androidx.lifecycle.LifecycleOwner;
import androidx.lifecycle.LifecycleRegistry;

import com.google.common.util.concurrent.ListenableFuture;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The camera, without an Activity — which is the only way a photo can be taken while the
 * phone is in a pocket.
 *
 * <p>Three platform facts shape this class, and each one is a thing that fails silently
 * if it is ignored:
 *
 * <ol>
 *   <li><b>CameraX needs a {@link LifecycleOwner}, and a Service is not one.</b> So this
 *       class <em>is</em> one: it owns a {@link LifecycleRegistry} and drives it to
 *       {@code RESUMED} itself. {@code bindToLifecycle} then has something to bind to,
 *       and releasing the lens is a lifecycle event rather than a leak.
 *   <li><b>Android 14+ blocks the camera for a background app even when the camera
 *       permission is granted.</b> The way through is a foreground service that declares
 *       and requests the {@code camera} service type, so this object is only ever used
 *       inside {@link VoiceService}, which holds that type — and
 *       {@code VoiceService.types()} only asks for the type when the runtime permission
 *       is actually there.
 *   <li><b>A capture must complete or fail visibly.</b> {@code takePicture} writes into a
 *       file with a callback, a background executor reads the JPEG back, and the temp
 *       file is deleted on <em>every</em> path — including the failure paths, because a
 *       photo of the owner's room is not something to leave lying in the cache.
 * </ol>
 *
 * <p>Deliberately not Camera2, for the same reason {@link CameraActivity} is not: CameraX
 * picks the lens, the resolution and the rotation, and a wrong guess at any of those on
 * an unlisted MIUI build is a black photo with no error attached.
 *
 * <p>All public methods are safe to call from any thread; the work is posted to the main
 * thread, which is where CameraX and the lifecycle registry require it.
 */
public final class CameraCapture implements LifecycleOwner {

    /** Every result is a callback, because every request is asynchronous. */
    public interface Sink {
        /** @param jpeg the encoded JPEG, ready to POST to {@code /see}. */
        void onCaptured(byte[] jpeg, int rotationDegrees);

        /** A named, printable reason.  Never a silent stop. */
        void onFailed(String detail);
    }

    private final Context context;
    private final LifecycleRegistry lifecycle;
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Runnable> waiting = new ArrayList<>();

    private ProcessCameraProvider provider;
    private ImageCapture capture;
    private boolean opening;
    private boolean open;
    private boolean released;

    public CameraCapture(Context context) {
        this.context = context.getApplicationContext();
        this.lifecycle = new LifecycleRegistry(this);
    }

    @NonNull
    @Override
    public Lifecycle getLifecycle() {
        return lifecycle;
    }

    public static boolean hasPermission(Context context) {
        return context.checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
    }

    public boolean isOpen() {
        return open;
    }

    /**
     * Bind the camera, then hand control to {@code then} on the main thread.
     *
     * <p>If a bind is already in flight the work is queued rather than started twice:
     * two concurrent {@code bindToLifecycle} calls race, and the loser's
     * {@code ImageCapture} silently never receives a frame.
     */
    private void whenOpen(final Sink sink, final Runnable then) {
        if (released) {
            sink.onFailed("the camera has been put away");
            return;
        }
        if (open && capture != null) {
            then.run();
            return;
        }
        if (!hasPermission(context)) {
            sink.onFailed("camera permission has not been granted");
            return;
        }
        waiting.add(then);
        if (opening) {
            return;
        }
        opening = true;
        final ListenableFuture<ProcessCameraProvider> future =
                ProcessCameraProvider.getInstance(context);
        future.addListener(() -> {
            ProcessCameraProvider resolved;
            try {
                resolved = future.get();
            } catch (Exception error) {
                failAll("the camera provider did not start: " + describe(error));
                return;
            }
            try {
                ImageCapture imageCapture = new ImageCapture.Builder()
                        // Minimise latency, not quality: this is a glance at a room,
                        // not a photograph, and Claire is waiting for the answer.
                        .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                        .build();
                lifecycle.setCurrentState(Lifecycle.State.RESUMED);
                resolved.unbindAll();
                // No Preview use case: there is no surface to draw on and nothing to
                // show.  ImageCapture alone is a legal, supported binding.
                resolved.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, imageCapture);
                provider = resolved;
                capture = imageCapture;
                open = true;
                opening = false;
                runWaiting();
            } catch (Exception error) {
                failAll("the camera would not bind: " + describe(error));
            }
        }, ContextCompat.getMainExecutor(context));
    }

    private void runWaiting() {
        List<Runnable> queued = new ArrayList<>(waiting);
        waiting.clear();
        for (Runnable job : queued) {
            job.run();
        }
    }

    private void failAll(String detail) {
        opening = false;
        open = false;
        capture = null;
        waiting.clear();
        lastFailure = detail;
    }

    private volatile String lastFailure = "";

    /** The reason the last bind attempt failed, or "".  For the UI to repeat. */
    public String lastFailure() {
        return lastFailure;
    }

    /**
     * Take one photo.  Opens the camera first if it is not already open, so a caller does
     * not have to sequence two asynchronous steps.
     */
    public void capture(final Sink sink) {
        main.post(() -> whenOpen(sink, () -> take(sink)));
    }

    private void take(final Sink sink) {
        final ImageCapture active = capture;
        if (active == null) {
            sink.onFailed("the camera is not open");
            return;
        }
        // In-memory capture, not a file in the cache directory, for two reasons that
        // happen to agree: CameraX hands back the rotation with an in-memory image and
        // only writes it into EXIF for a file, and a photo of the owner's surroundings is
        // not something to leave on flash even briefly.  The bytes exist in this array
        // and nowhere else.
        active.takePicture(io, new ImageCapture.OnImageCapturedCallback() {
            @Override
            public void onCaptureSuccess(@NonNull ImageProxy image) {
                int rotation = image.getImageInfo().getRotationDegrees();
                byte[] bytes = null;
                try {
                    ByteBuffer buffer = image.getPlanes()[0].getBuffer();
                    bytes = new byte[buffer.remaining()];
                    buffer.get(bytes);
                } catch (Exception error) {
                    sink.onFailed("the photo could not be read: " + error.getMessage());
                } finally {
                    image.close();
                }
                if (bytes == null || bytes.length == 0) {
                    sink.onFailed("the camera produced an empty photo");
                } else {
                    sink.onCaptured(bytes, rotation);
                }
            }

            @Override
            public void onError(@NonNull ImageCaptureException error) {
                sink.onFailed("the camera refused the capture: " + describe(error));
            }
        });
    }

    /** Unbind and let go of the lens.  Idempotent, safe from any thread. */
    public void release() {
        main.post(() -> {
            released = true;
            open = false;
            opening = false;
            capture = null;
            waiting.clear();
            if (provider != null) {
                try {
                    provider.unbindAll();
                } catch (Exception ignored) {
                    // Unbinding an unbound provider is not an error.
                }
                provider = null;
            }
            if (lifecycle.getCurrentState() != Lifecycle.State.DESTROYED) {
                lifecycle.setCurrentState(Lifecycle.State.DESTROYED);
            }
        });
    }

    /** Stop the executor.  Called from the service's onDestroy, after release(). */
    public void shutdown() {
        io.shutdownNow();
    }

    private static String describe(Exception error) {
        String message = error.getMessage();
        return error.getClass().getSimpleName() + (message == null ? "" : ": " + message);
    }
}
